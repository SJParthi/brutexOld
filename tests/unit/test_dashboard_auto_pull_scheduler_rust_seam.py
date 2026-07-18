"""Wave-3G Rust kernel path — ``brutex/dashboard/auto_pull_scheduler.py`` (DEC-RUST-PULL-STATE-001).

Flip phase: the tick ladder + the durable-state lattice ARE the kernel — no
flag, no oracle.

* the TWO-STAGE tick consult preserves the LAZY gate/launcher evaluation
  order (stage 1 passes placeholder falses; stage 2 re-consults with the
  real facts) and the kernel decision is SERVED (deliberately-wrong fakes
  prove the serving);
* stage-IMPOSSIBLE answers (FIRE/LAUNCHER_UNBOUND from placeholders; a
  state arm — or FIRE with no launcher — at stage 2) are REFUSED loudly;
* the state-parse lattice serves kernel refusals as the byte-identical
  pre-flip ``ValueError`` messages; VALID constructs mechanically, with the
  poisoned-VALID consistency gates refusing crash-shaped wheel faults;
* every wheel fault RAISES :class:`PullStateKernelError`; a still-SET
  removed ``BRUTEX_PULL_STATE_RUST`` knob raises
  :class:`PullStateFlagRemovedError` — incl. out of
  :meth:`AutoPullStateStore.load` on a PRESENT decodable sidecar (an ABSENT
  sidecar never consults the kernel);
* the REAL wheel replays the frozen golden lattice bit-for-bit.
"""

from __future__ import annotations

import json
import re
import sys
from base64 import b64decode
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import brutex_pull_state
import pytest
from brutex import pull_state_rust
from brutex.calendars.auto_pull_gate import AutoGateVerdict
from brutex.calendars.settle_evidence import SETTLE_EVIDENCE_PATH_ENV
from brutex.dashboard.auto_pull_scheduler import (
    AutoLaunchResult,
    AutoPullScheduler,
    AutoPullState,
    AutoPullStateStore,
    _classify_state_for_kernel,
    _state_from_payload,
    _state_refusal_text,
)
from structlog.testing import capture_logs

_INSTANT = datetime(2026, 7, 13, 18, 0)
_TODAY = date(2026, 7, 13)
_ENV = {SETTLE_EVIDENCE_PATH_ENV: "off"}

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "golden"
    / "fixtures"
    / "auto_pull_scheduler_verdicts.json"
)


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    """A ``sys.modules`` plant standing in for the ``brutex_pull_state`` wheel.

    Symbols not scripted by the test DELEGATE to the REAL wheel (captured at
    import) — post-flip the state LOAD consults ``auto_state_verdict`` on
    every saved-store read, so a tick-focused fake must still parse state.
    """
    functions.setdefault("auto_state_verdict", brutex_pull_state.auto_state_verdict)
    functions.setdefault("auto_tick_decision", brutex_pull_state.auto_tick_decision)
    return SimpleNamespace(**functions)


class _CountingGate:
    """A counting gate fake with a scripted fixed verdict."""

    def __init__(self, *, allowed: bool = True) -> None:
        self.calls = 0
        self._verdict = (
            AutoGateVerdict(allowed=True, reason="ok", detail="ok")
            if allowed
            else AutoGateVerdict(allowed=False, reason="before_settle", detail="wait for settle")
        )

    def __call__(self, _instant: datetime) -> AutoGateVerdict:
        self.calls += 1
        return self._verdict


class _CountingLauncher:
    """A counting launcher fake with a scripted fixed outcome."""

    def __init__(self, *, started: bool = True) -> None:
        self.calls = 0
        self._result = AutoLaunchResult(started=started, detail="scripted")

    def __call__(self) -> AutoLaunchResult:
        self.calls += 1
        return self._result


def _scheduler(
    tmp_path: Path,
    *,
    state: AutoPullState | None = None,
    gate_allowed: bool = True,
    launcher: _CountingLauncher | None = None,
) -> tuple[AutoPullScheduler, _CountingGate, AutoPullStateStore]:
    """A scheduler over a scratch store with counting gate/launcher fakes."""
    store = AutoPullStateStore(tmp_path)
    if state is not None:
        store.save(state)
    gate = _CountingGate(allowed=gate_allowed)
    scheduler = AutoPullScheduler(
        store, launcher=launcher, gate=gate, tick_seconds=3600.0, env=dict(_ENV)
    )
    return scheduler, gate, store


def _armed_fresh() -> AutoPullState:
    """An armed state with no marks for today."""
    return AutoPullState(armed=True, armed_at="2026-07-01T10:00:00")


# ── the kernel decision is SERVED (deliberately-wrong fakes) ─────────────────


class TestKernelServesTick:
    """The tick ladder is the kernel's — served, never re-derived."""

    @pytest.mark.parametrize(
        ("code", "token"),
        [(0, "disarmed"), (1, "already_succeeded"), (2, "already_attempted")],
    )
    def test_pre_gate_arm_served_without_a_gate_consult(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int, token: str
    ) -> None:
        """A deliberately-WRONG stage-1 answer is served — and the injected
        gate is NEVER consulted (the laziness law under the kernel path)."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_tick_decision=lambda *a: code),
        )
        launcher = _CountingLauncher()
        scheduler, gate, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == token
        assert (gate.calls, launcher.calls) == (0, 0)

    def test_two_stage_consult_packs_placeholders_then_real_facts(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stage 1 packs placeholder falses; stage 2 the real gate/wiring facts."""
        seen: list[tuple[object, ...]] = []

        def kernel(*args: object) -> int:
            seen.append(args)
            return 3 if len(seen) == 1 else 5

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        launcher = _CountingLauncher()
        scheduler, gate, store = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "fired"
        assert seen == [
            (True, False, False, False, False),  # stage 1: placeholders
            (True, False, False, True, True),  # stage 2: the real facts
        ]
        assert (gate.calls, launcher.calls) == (1, 1)
        assert store.load().last_attempt_date == _TODAY.isoformat()

    def test_deferred_served_never_calls_the_launcher(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A DEFERRED stage-2 answer defers with zero launcher calls."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_tick_decision=lambda *a: 3),
        )
        launcher = _CountingLauncher()
        scheduler, gate, store = _scheduler(
            tmp_path, state=_armed_fresh(), gate_allowed=False, launcher=launcher
        )
        assert scheduler.tick_once(now=_INSTANT) == "deferred:before_settle"
        assert (gate.calls, launcher.calls) == (1, 0)
        assert store.load().last_attempt_date is None

    def test_fire_served_refused_launcher_marks_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FIRE is the kernel's; the fired-vs-refused split stays Python."""

        def kernel(*args: object) -> int:
            return 3 if args[3] is False else 5

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        launcher = _CountingLauncher(started=False)
        scheduler, _, store = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "refused_retry"
        assert launcher.calls == 1
        assert store.load().last_attempt_date is None

    def test_unbound_served_on_stage_two(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """LAUNCHER_UNBOUND at stage 2 with no launcher reads refused_retry."""

        def kernel(*args: object) -> int:
            return 3 if args[4] is False and args[3] is False else 4

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        scheduler, gate, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=None)
        assert scheduler.tick_once(now=_INSTANT) == "refused_retry"
        assert gate.calls == 1

    def test_unbound_answer_with_a_bound_launcher_stays_the_refused_serve(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A LAUNCHER_UNBOUND stage-2 answer against a BOUND launcher is the
        pre-flip-compatible SAFE serve: refused_retry, no launcher call, no
        attempt marked (an inconsistent-but-harmless wheel answer)."""
        calls: list[int] = []

        def kernel(*_a: object) -> int:
            calls.append(0)
            return 3 if len(calls) == 1 else 4

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        launcher = _CountingLauncher()
        scheduler, _, store = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "refused_retry"
        assert launcher.calls == 0
        assert store.load().last_attempt_date is None


# ── fail-closed: wheel faults + stage-impossible answers REFUSE ──────────────


class TestTickFailClosed:
    """Every tick-path wheel fault raises the typed error — no oracle."""

    def test_missing_wheel_raises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """An unimportable wheel refuses the tick loudly (``wheel_missing``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        scheduler, gate, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            scheduler.tick_once(now=_INSTANT)
        assert gate.calls == 0

    def test_stale_wheel_raises_naming_the_symbol(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A wheel predating the symbol refuses naming it (``wheel_stale``)."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", _RecordingBomb())
        scheduler, _, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="auto_tick_decision"):
            scheduler.tick_once(now=_INSTANT)

    def test_raising_kernel_raises_kernel_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A raising kernel becomes the typed ``kernel_error`` raise."""

        def kernel(*_a: object) -> int:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        scheduler, _, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_error"):
            scheduler.tick_once(now=_INSTANT)

    @pytest.mark.parametrize("poison", [lambda *a: 7, lambda *a: True, lambda *a: "fired"])
    def test_out_of_vocabulary_stage_one_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, poison: Any
    ) -> None:
        """Out-of-vocabulary stage-1 answers are refused by the facade gate."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=poison)
        )
        scheduler, gate, _ = _scheduler(
            tmp_path, state=_armed_fresh(), launcher=_CountingLauncher()
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            scheduler.tick_once(now=_INSTANT)
        assert gate.calls == 0

    @pytest.mark.parametrize("code", [4, 5])
    def test_stage_impossible_placeholder_answer_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int
    ) -> None:
        """FIRE/LAUNCHER_UNBOUND from placeholder falses is stage-IMPOSSIBLE
        (the real ladder cannot produce it) — refused, never fired blind."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=lambda *a: code)
        )
        launcher = _CountingLauncher()
        scheduler, gate, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        with pytest.raises(
            pull_state_rust.PullStateKernelError, match="pre-gate placeholder stage"
        ):
            scheduler.tick_once(now=_INSTANT)
        assert (gate.calls, launcher.calls) == (0, 0)

    def test_stage_two_state_arm_answer_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stage 2 answering a STATE arm (0/1/2) contradicts stage 1 over the
        SAME state facts — refused loudly."""
        calls: list[int] = []

        def kernel(*_a: object) -> int:
            calls.append(0)
            return 3 if len(calls) == 1 else 0

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        launcher = _CountingLauncher()
        scheduler, _, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        with pytest.raises(pull_state_rust.PullStateKernelError, match="post-gate stage"):
            scheduler.tick_once(now=_INSTANT)
        assert launcher.calls == 0

    def test_stage_two_fire_with_no_launcher_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """FIRE against ``launcher_bound=False`` contradicts the wiring fact
        the kernel was handed — refused, there is nothing to call."""
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_tick_decision=lambda *a: 3 if a[3] is False else 5),
        )
        scheduler, _, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=None)
        with pytest.raises(pull_state_rust.PullStateKernelError, match="post-gate stage"):
            scheduler.tick_once(now=_INSTANT)

    def test_set_removed_flag_refuses(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A still-SET removed knob refuses the tick — truthy AND falsy alike."""
        scheduler, _, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        for value in ("1", "0"):
            monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, value)
            with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
                scheduler.tick_once(now=_INSTANT)

    def test_run_loop_folds_the_kernel_fault_into_tick_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The never-crash-loop law survives the flip: a kernel fault inside
        the daemon loop's tick folds into ONE deduped ``auto_pull.tick_error``
        WARN (via ``_note_tick_error``), never a crashed loop."""
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        scheduler, _, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        with capture_logs() as logs:
            try:
                scheduler.tick_once(now=_INSTANT)
            except Exception as exc:  # the loop guard's own catch, mirrored
                scheduler._note_tick_error(exc)
        events = [e for e in logs if e["event"] == "auto_pull.tick_error"]
        assert len(events) == 1
        assert events[0]["reason"] == "PullStateKernelError"
        assert "wheel_missing" in scheduler.status(now=_INSTANT)["last_outcome"]


# ── the state-parse plane ────────────────────────────────────────────────────


class TestStateParse:
    """The durable-state lattice is the kernel's; messages stay Python."""

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            ([1], "not a JSON object"),
            ({"schema": 2, "armed": True}, "unknown schema 2 (expected 1)"),
            ({"schema": "1", "armed": True}, "unknown schema '1' (expected 1)"),
            ({"schema": 1, "armed": 1}, "armed is not a bool"),
            ({"schema": 1, "armed": True, "armed_at": 7}, "armed_at is not a string"),
            (
                {"schema": 1, "armed": True, "last_attempt_date": []},
                "last_attempt_date is not a string",
            ),
            (
                {"schema": 1, "armed": True, "last_success_date": {}},
                "last_success_date is not a string",
            ),
        ],
    )
    def test_refusal_messages_byte_identical_over_the_real_wheel(
        self, payload: object, message: str
    ) -> None:
        """The REAL kernel's refusal per shape composes the byte-identical
        pre-flip ``ValueError`` message."""
        with pytest.raises(ValueError, match="^" + re.escape(message) + "$"):
            _state_from_payload(payload)

    def test_kernel_lattice_matches_the_inline_law(self) -> None:
        """The real kernel verdict vs an INLINE test-code twin of the lattice
        (test code may reimplement the law; production may not) over the
        classified codes of a hostile payload matrix."""

        def inline_law(codes: dict[str, int | bool]) -> int:
            if not codes["root_mapping"]:
                return 1
            ok = (codes["schema_kind"] in (1, 2) and codes["schema_i64"] == 1) or (
                codes["schema_kind"] == 4 and codes["schema_f64_bits"] == 0x3FF0000000000000
            )
            if not ok:
                return 2
            if not codes["armed_is_bool"]:
                return 3
            for offset, key in enumerate(
                ("armed_at_kind", "last_attempt_kind", "last_success_kind")
            ):
                if codes[key] == 3:
                    return 4 + offset
            return 0

        payloads: list[object] = [
            {"schema": 1, "armed": True},
            {"schema": True, "armed": False},
            {"schema": 1.0, "armed": False},
            {"schema": 2, "armed": True},
            {"schema": 2**70, "armed": True},
            {"schema": 1, "armed": 1},
            {"schema": 1, "armed": True, "armed_at": 9, "last_success_date": 9},
            {"schema": 1, "armed": True, "last_attempt_date": []},
            [1],
            "junk",
        ]
        for payload in payloads:
            codes = _classify_state_for_kernel(payload)
            got = pull_state_rust.rust_auto_state_verdict(
                root_mapping=bool(codes["root_mapping"]),
                schema_kind=int(codes["schema_kind"]),
                schema_i64=int(codes["schema_i64"]),
                schema_f64_bits=int(codes["schema_f64_bits"]),
                armed_is_bool=bool(codes["armed_is_bool"]),
                armed_at_kind=int(codes["armed_at_kind"]),
                last_attempt_kind=int(codes["last_attempt_kind"]),
                last_success_kind=int(codes["last_success_kind"]),
            )
            assert got == inline_law(codes), payload

    def test_valid_constructs_mechanically(self) -> None:
        """A VALID verdict extracts the state with no Python re-validation."""
        state = _state_from_payload({"schema": 1, "armed": True, "last_success_date": "2026-07-12"})
        assert state == AutoPullState(armed=True, last_success_date="2026-07-12")

    def test_poisoned_valid_on_corrupt_payload_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel wrongly answering VALID for a non-bool ``armed`` is
        refused by the consistency gate — the corrupt file can never arm the
        scheduler, and no junk state is ever constructed."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 0)
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="inconsistent"):
            _state_from_payload({"schema": 1, "armed": 1})

    def test_poisoned_valid_on_non_mapping_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A VALID answer for a non-mapping root is the same refusal."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 0)
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="inconsistent"):
            _state_from_payload([1])

    def test_inconsistent_refusal_codes_still_refuse_the_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poisoned in-vocabulary refusal still REFUSES (fail-safe: the
        file reads corrupt ⇒ DISARMED downstream) — a NOT_OBJECT answer for
        a mapping raises its message; a mapping-shaped refusal for a
        NON-mapping folds to the not-a-JSON-object truth."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 1)
        )
        with pytest.raises(ValueError, match="not a JSON object"):
            _state_from_payload({"schema": 1, "armed": False})
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 2)
        )
        with pytest.raises(ValueError, match="not a JSON object"):
            _state_from_payload([1])

    @pytest.mark.parametrize("poison", [lambda *a: 255, lambda *a: True, lambda *a: b"0"])
    def test_poison_verdicts_raise_kernel_invalid(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        """Out-of-vocabulary verdicts are refused by the facade gate."""
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=poison)
        )
        with pytest.raises(pull_state_rust.PullStateKernelError, match="kernel_invalid"):
            _state_from_payload({"schema": 1, "armed": True})

    def test_refusal_text_totality(self) -> None:
        """``_state_refusal_text`` is total over the served refusal codes."""
        assert _state_refusal_text(pull_state_rust.STATE_CODE_NOT_OBJECT, [1]) == (
            "not a JSON object"
        )
        assert _state_refusal_text(pull_state_rust.STATE_CODE_BAD_ARMED_AT, "junk") == (
            "not a JSON object"  # a field code for a non-mapping folds honestly
        )
        assert (
            _state_refusal_text(pull_state_rust.STATE_CODE_BAD_LAST_SUCCESS, {"schema": 1})
            == "last_success_date is not a string"
        )


class TestStoreLoad:
    """The corruption-tolerant store posture across the flip."""

    def test_store_corrupt_posture_preserved(self, tmp_path: Path) -> None:
        """Corrupt ⇒ DISARMED + ONE ``auto_pull.state_corrupt`` WARN — the
        round-6/7 posture, now kernel-served."""
        store = AutoPullStateStore(tmp_path)
        store.path.write_text('{"schema": 9, "armed": true}', encoding="utf-8")
        with capture_logs() as logs:
            assert store.load() == AutoPullState()
        corrupt = [e for e in logs if e["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["detail"] == "unknown schema 9 (expected 1)"

    def test_absent_sidecar_never_consults_the_kernel(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An ABSENT state file loads DISARMED with ZERO kernel touches —
        the common never-armed boot needs no wheel at all."""
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert AutoPullStateStore(tmp_path).load() == AutoPullState()
        assert bomb.touched == []

    def test_present_sidecar_with_missing_wheel_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A PRESENT decodable sidecar + a missing wheel raises the typed
        error out of ``load()`` — fail-closed, never a silent DISARM."""
        store = AutoPullStateStore(tmp_path)
        store.path.write_text('{"schema": 1, "armed": true}', encoding="utf-8")
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        with pytest.raises(pull_state_rust.PullStateKernelError, match="wheel_missing"):
            store.load()

    def test_set_removed_flag_reraises_past_the_corrupt_fold(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``PullStateFlagRemovedError`` subclasses ``ValueError`` but is
        explicitly RE-RAISED past the corrupt ⇒ DISARMED fold — a stale
        deployment config fails LOUD, never reads as a corrupt file."""
        store = AutoPullStateStore(tmp_path)
        store.path.write_text('{"schema": 1, "armed": true}', encoding="utf-8")
        monkeypatch.setenv(pull_state_rust.PULL_STATE_RUST_ENV, "1")
        with pytest.raises(pull_state_rust.PullStateFlagRemovedError):
            store.load()


class TestClassifier:
    """The mechanical classifier — type tags only, no law."""

    @pytest.mark.parametrize(
        ("payload", "kind", "i64", "bits"),
        [
            ({}, pull_state_rust.SCHEMA_KIND_MISSING, 0, 0),
            ({"schema": None}, pull_state_rust.SCHEMA_KIND_MISSING, 0, 0),
            ({"schema": True}, pull_state_rust.SCHEMA_KIND_BOOL, 1, 0),
            ({"schema": 1}, pull_state_rust.SCHEMA_KIND_INT, 1, 0),
            ({"schema": 2**70}, pull_state_rust.SCHEMA_KIND_INT_HUGE, 0, 0),
            ({"schema": -(2**70)}, pull_state_rust.SCHEMA_KIND_INT_HUGE, 0, 0),
            ({"schema": 1.0}, pull_state_rust.SCHEMA_KIND_FLOAT, 0, 0x3FF0000000000000),
            ({"schema": "1"}, pull_state_rust.SCHEMA_KIND_OTHER, 0, 0),
        ],
    )
    def test_schema_kinds(self, payload: dict[str, Any], kind: int, i64: int, bits: int) -> None:
        """Every schema concrete-type arm classifies mechanically."""
        codes = _classify_state_for_kernel(payload)
        assert (codes["schema_kind"], codes["schema_i64"], codes["schema_f64_bits"]) == (
            kind,
            i64,
            bits,
        )

    def test_field_kinds_and_armed(self) -> None:
        """The optional-string field tags + the armed bool tag."""
        codes = _classify_state_for_kernel(
            {"schema": 1, "armed": False, "armed_at": None, "last_attempt_date": "x"}
        )
        assert codes["armed_is_bool"] is True
        assert codes["armed_at_kind"] == pull_state_rust.STATE_FIELD_NULL
        assert codes["last_attempt_kind"] == pull_state_rust.STATE_FIELD_STR
        assert codes["last_success_kind"] == pull_state_rust.STATE_FIELD_ABSENT
        codes = _classify_state_for_kernel({"armed": "x", "armed_at": 7})
        assert codes["armed_is_bool"] is False
        assert codes["armed_at_kind"] == pull_state_rust.STATE_FIELD_OTHER

    def test_non_mapping_root_is_total(self) -> None:
        """A non-mapping payload classifies as the zeroed root=False codes."""
        codes = _classify_state_for_kernel([1, 2])
        assert codes["root_mapping"] is False
        assert codes["armed_is_bool"] is False
        assert codes["armed_at_kind"] == pull_state_rust.STATE_FIELD_ABSENT


# ── the REAL wheel golden replay ─────────────────────────────────────────────


class TestRealWheelGoldenReplay:
    """The installed wheel replays the frozen lattice bit-for-bit."""

    def test_tick_lattice_replays_bit_for_bit(self) -> None:
        """Every frozen tick row through the RAW wheel symbol."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in payload["tick"]:
            got = brutex_pull_state.auto_tick_decision(
                row["armed"],
                row["succeeded_today"],
                row["attempted_today"],
                row["gate_allowed"],
                row["launcher_bound"],
            )
            assert got == row["code"], row["name"]

    def test_state_corpus_replays_bit_for_bit(self) -> None:
        """Every frozen kernel state row through the RAW wheel symbol."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        replayed = 0
        for row in payload["state"]:
            if not row.get("kernel_row"):
                continue
            c = row["classify"]
            got = brutex_pull_state.auto_state_verdict(
                c["root_mapping"],
                c["schema_kind"],
                c["schema_i64"],
                c["schema_f64_bits"],
                c["armed_is_bool"],
                c["armed_at_kind"],
                c["last_attempt_kind"],
                c["last_success_kind"],
            )
            assert got == row["verdict"], row["name"]
            replayed += 1
        assert replayed >= 26

    def test_store_load_matches_golden_loads(self, tmp_path: Path) -> None:
        """Every frozen state BODY loads to the recorded state through the
        LIVE (kernel-only) store."""
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for n, row in enumerate(payload["state"]):
            root = tmp_path / f"case-{n}"
            root.mkdir()
            store = AutoPullStateStore(root)
            store.path.write_bytes(b64decode(row["body_b64"]))
            loaded = store.load()
            assert {
                "armed": loaded.armed,
                "armed_at": loaded.armed_at,
                "last_attempt_date": loaded.last_attempt_date,
                "last_success_date": loaded.last_success_date,
            } == row["loaded"], row["name"]
