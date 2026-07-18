"""Wave-3G Rust seam — ``brutex/dashboard/auto_pull_scheduler.py`` (DEC-RUST-PULL-STATE-001).

The auto-pull-scheduler tick-decision + durable-state-parse seam behind the
default-OFF ``BRUTEX_PULL_STATE_RUST``:

* flag OFF is byte-identical and NEVER touches the kernel (the recording
  AttributeError bomb — with teeth: a flag-ON consult genuinely records);
* flag ON serves the kernel's decision (deliberately-WRONG fakes prove the
  serving) through the TWO-STAGE consult that preserves the oracle's lazy
  gate/launcher evaluation order (stage 1 passes placeholder falses; stage 2
  re-consults with the real facts);
* every degrade (raising kernel, poison verdicts, stage-inconsistent codes,
  wheel missing) falls back to the byte-identical Python oracle;
* the state-parse seam serves kernel REFUSALS as byte-identical
  ``ValueError`` messages, VALID falls through to the oracle (fail-safe),
  and the corrupt ⇒ DISARMED + one WARN store posture is preserved;
* the REAL wheel (capability-probed — the installed wheel may predate the
  Wave-3G symbols) replays the frozen golden lattice bit-for-bit.
"""

from __future__ import annotations

import json
import sys
from base64 import b64decode
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    _rust_state_refusal,
    _state_from_payload,
)
from structlog.testing import capture_logs

_FLAG = pull_state_rust.PULL_STATE_RUST_ENV
_INSTANT = datetime(2026, 7, 13, 18, 0)
_TODAY = date(2026, 7, 13)
_ENV = {SETTLE_EVIDENCE_PATH_ENV: "off"}

_JSON_FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "golden"
    / "fixtures"
    / "auto_pull_scheduler_verdicts.json"
)


@pytest.fixture(autouse=True)
def _clean_warn_registry() -> Iterator[None]:
    pull_state_rust.reset_for_tests()
    yield
    pull_state_rust.reset_for_tests()


class _RecordingBomb:
    """A ``sys.modules`` plant that RECORDS every attribute touch."""

    def __init__(self) -> None:
        self.touched: list[str] = []

    def __getattr__(self, name: str) -> object:
        object.__getattribute__(self, "touched").append(name)
        raise AttributeError(name)


def _fake_kernel(**functions: object) -> SimpleNamespace:
    return SimpleNamespace(**functions)


class _CountingGate:
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
    store = AutoPullStateStore(tmp_path)
    if state is not None:
        store.save(state)
    gate = _CountingGate(allowed=gate_allowed)
    scheduler = AutoPullScheduler(
        store, launcher=launcher, gate=gate, tick_seconds=3600.0, env=dict(_ENV)
    )
    return scheduler, gate, store


def _armed_fresh() -> AutoPullState:
    return AutoPullState(armed=True, armed_at="2026-07-01T10:00:00")


# ── flag OFF: byte-identical + never imports (the bomb spy) ──────────────────


class TestFlagOffNeverTouchesKernel:
    def test_tick_and_state_paths_flag_unset(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv(_FLAG, raising=False)
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        launcher = _CountingLauncher()
        scheduler, gate, store = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "fired"
        assert (gate.calls, launcher.calls) == (1, 1)
        assert _state_from_payload({"schema": 1, "armed": False}) == AutoPullState()
        with pytest.raises(ValueError, match="not a JSON object"):
            _state_from_payload([1])
        store.path.write_text("{junk", encoding="utf-8")
        assert AutoPullStateStore(tmp_path).load() == AutoPullState()
        assert bomb.touched == []

    def test_explicit_falsy_flag_also_short_circuits(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "0")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        scheduler, gate, _ = _scheduler(tmp_path)
        assert scheduler.tick_once(now=_INSTANT) == "disarmed"
        assert gate.calls == 0
        assert bomb.touched == []


class TestBombTeeth:
    def test_flag_on_tick_records_the_touch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        scheduler, _gate, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        # The AttributeError bomb reads as a stale wheel → the oracle serves.
        assert scheduler.tick_once(now=_INSTANT) == "disarmed"
        assert "auto_tick_decision" in bomb.touched

    def test_flag_on_state_parse_records_the_touch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_FLAG, "1")
        bomb = _RecordingBomb()
        monkeypatch.setitem(sys.modules, "brutex_pull_state", bomb)
        assert _state_from_payload({"schema": 1, "armed": True}) == AutoPullState(armed=True)
        assert "auto_state_verdict" in bomb.touched


# ── flag ON: the kernel decision is SERVED (deliberately-wrong fakes) ────────


class TestFlagOnServesTick:
    @pytest.mark.parametrize(
        ("code", "token"),
        [(0, "disarmed"), (1, "already_succeeded"), (2, "already_attempted")],
    )
    def test_pre_gate_arm_served_without_a_gate_consult(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int, token: str
    ) -> None:
        """A deliberately-WRONG stage-1 answer is served — and the injected
        gate is NEVER consulted (the laziness law under the seam)."""
        monkeypatch.setenv(_FLAG, "1")
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
        monkeypatch.setenv(_FLAG, "1")
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
        monkeypatch.setenv(_FLAG, "1")
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
        monkeypatch.setenv(_FLAG, "1")

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
        monkeypatch.setenv(_FLAG, "1")

        def kernel(*args: object) -> int:
            return 3 if args[4] is False and args[3] is False else 4

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        scheduler, gate, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=None)
        assert scheduler.tick_once(now=_INSTANT) == "refused_retry"
        assert gate.calls == 1


class TestFlagOnTickDegrades:
    @pytest.mark.parametrize(
        "poison",
        [lambda *a: 7, lambda *a: True, lambda *a: "fired", lambda *a: None],
    )
    def test_stage_one_poison_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, poison: Any
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=poison)
        )
        launcher = _CountingLauncher()
        scheduler, gate, store = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "fired"
        assert (gate.calls, launcher.calls) == (1, 1)
        assert store.load().last_attempt_date == _TODAY.isoformat()

    def test_raising_kernel_degrades_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")

        def kernel(*_a: object) -> int:
            raise RuntimeError("kernel bomb")

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        scheduler, gate, _ = _scheduler(tmp_path, launcher=_CountingLauncher())
        assert scheduler.tick_once(now=_INSTANT) == "disarmed"
        assert gate.calls == 0

    def test_stage_two_state_arm_answer_is_inconsistent_and_degrades(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Stage 2 answering a STATE arm (0/1/2) contradicts stage 1 — the
        oracle's post-gate decision serves instead."""
        monkeypatch.setenv(_FLAG, "1")
        calls: list[int] = []

        def kernel(*_a: object) -> int:
            calls.append(0)
            return 3 if len(calls) == 1 else 0

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=kernel)
        )
        launcher = _CountingLauncher()
        scheduler, _, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=launcher)
        assert scheduler.tick_once(now=_INSTANT) == "fired"
        assert launcher.calls == 1

    def test_stage_two_fire_with_no_launcher_degrades_to_unbound(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules,
            "brutex_pull_state",
            _fake_kernel(auto_tick_decision=lambda *a: 3 if a[3] is False else 5),
        )
        scheduler, _, _ = _scheduler(tmp_path, state=_armed_fresh(), launcher=None)
        assert scheduler.tick_once(now=_INSTANT) == "refused_retry"


# ── flag ON: the state-parse seam ────────────────────────────────────────────


class TestFlagOnServesStateParse:
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
    def test_refusal_messages_byte_identical_to_oracle(
        self, monkeypatch: pytest.MonkeyPatch, payload: object, message: str
    ) -> None:
        """The REAL kernel law is mirrored by a faithful fake here (the real
        wheel replays the frozen corpus below); the seam's message per code
        must equal the oracle's byte-for-byte."""
        monkeypatch.delenv(_FLAG, raising=False)
        with pytest.raises(ValueError) as oracle_exc:
            _state_from_payload(payload)
        monkeypatch.setenv(_FLAG, "1")

        def kernel(*args: object) -> int:
            # A faithful in-test twin of the kernel lattice over the codes.
            (root, s_kind, s_i64, s_bits, armed_b, k_a, k_la, k_ls) = args
            if not root:
                return 1
            ok = (s_kind in (1, 2) and s_i64 == 1) or (s_kind == 4 and s_bits == 0x3FF0000000000000)
            if not ok:
                return 2
            if not armed_b:
                return 3
            for offset, kind in enumerate((k_a, k_la, k_ls)):
                if kind == 3:
                    return 4 + offset
            return 0

        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=kernel)
        )
        with pytest.raises(ValueError) as served_exc:
            _state_from_payload(payload)
        assert str(served_exc.value) == str(oracle_exc.value) == message

    def test_valid_falls_through_to_the_oracle_construction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 0)
        )
        state = _state_from_payload({"schema": 1, "armed": True, "last_success_date": "2026-07-12"})
        assert state == AutoPullState(armed=True, last_success_date="2026-07-12")

    def test_valid_poison_on_corrupt_payload_is_fail_safe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kernel wrongly answering VALID falls through to the oracle,
        which still refuses — the corrupt file can never arm the scheduler."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 0)
        )
        with pytest.raises(ValueError, match="armed is not a bool"):
            _state_from_payload({"schema": 1, "armed": 1})

    def test_inconsistent_refusals_degrade_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        # NOT_OBJECT for a mapping payload → oracle (which accepts it).
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 1)
        )
        assert _state_from_payload({"schema": 1, "armed": False}) == AutoPullState()
        # A mapping-shaped refusal for a NON-mapping payload → oracle.
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 2)
        )
        with pytest.raises(ValueError, match="not a JSON object"):
            _state_from_payload([1])

    @pytest.mark.parametrize("poison", [lambda *a: 255, lambda *a: True, lambda *a: b"0"])
    def test_poison_verdicts_degrade_to_the_oracle(
        self, monkeypatch: pytest.MonkeyPatch, poison: Any
    ) -> None:
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=poison)
        )
        assert _state_from_payload({"schema": 1, "armed": True}) == AutoPullState(armed=True)

    def test_store_corrupt_posture_preserved_flag_on(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Corrupt ⇒ DISARMED + ONE ``auto_pull.state_corrupt`` WARN — the
        round-6/7 posture, byte-preserved under the seam."""
        monkeypatch.setenv(_FLAG, "1")
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 2)
        )
        store = AutoPullStateStore(tmp_path)
        store.path.write_text('{"schema": 9, "armed": true}', encoding="utf-8")
        with capture_logs() as logs:
            assert store.load() == AutoPullState()
        corrupt = [e for e in logs if e["event"] == "auto_pull.state_corrupt"]
        assert len(corrupt) == 1
        assert corrupt[0]["detail"] == "unknown schema 9 (expected 1)"


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
        codes = _classify_state_for_kernel(payload)
        assert (codes["schema_kind"], codes["schema_i64"], codes["schema_f64_bits"]) == (
            kind,
            i64,
            bits,
        )

    def test_field_kinds_and_armed(self) -> None:
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


# ── the facade accessors ─────────────────────────────────────────────────────


class TestFacadeTickAccessor:
    def test_valid_and_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=lambda *a: 5)
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            )
            == 5
        )
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_tick_decision=lambda *a: 6)
        )
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=True,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=True,
                launcher_bound=True,
            )
            is None
        )

    def test_wheel_missing_degrades(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "brutex_pull_state", None)
        assert (
            pull_state_rust.rust_auto_tick_decision(
                armed=False,
                succeeded_today=False,
                attempted_today=False,
                gate_allowed=False,
                launcher_bound=False,
            )
            is None
        )


class TestFacadeStateAccessor:
    def test_valid_and_poison(self, monkeypatch: pytest.MonkeyPatch) -> None:
        kwargs: dict[str, Any] = {
            "root_mapping": True,
            "schema_kind": pull_state_rust.SCHEMA_KIND_INT,
            "schema_i64": 1,
            "schema_f64_bits": 0,
            "armed_is_bool": True,
            "armed_at_kind": pull_state_rust.STATE_FIELD_ABSENT,
            "last_attempt_kind": pull_state_rust.STATE_FIELD_ABSENT,
            "last_success_kind": pull_state_rust.STATE_FIELD_ABSENT,
        }
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 0)
        )
        assert pull_state_rust.rust_auto_state_verdict(**kwargs) == 0
        # 255 (the kernel's input-mismatch answer) is OUT of the served
        # vocabulary — kernel_invalid, oracle decides.
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 255)
        )
        assert pull_state_rust.rust_auto_state_verdict(**kwargs) is None
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: False)
        )
        assert pull_state_rust.rust_auto_state_verdict(**kwargs) is None


# ── the REAL wheel (capability-probed) ───────────────────────────────────────


def _real_kernel() -> Any:
    wheel = pytest.importorskip("brutex_pull_state")
    if not hasattr(wheel, "auto_tick_decision") or not hasattr(wheel, "auto_state_verdict"):
        pytest.skip("installed brutex_pull_state wheel predates the Wave-3G symbols")
    return wheel


class TestRealWheelGoldenReplay:
    def test_tick_lattice_replays_bit_for_bit(self) -> None:
        wheel = _real_kernel()
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        for row in payload["tick"]:
            got = wheel.auto_tick_decision(
                row["armed"],
                row["succeeded_today"],
                row["attempted_today"],
                row["gate_allowed"],
                row["launcher_bound"],
            )
            assert got == row["code"], row["name"]

    def test_state_corpus_replays_bit_for_bit(self) -> None:
        wheel = _real_kernel()
        payload = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
        replayed = 0
        for row in payload["state"]:
            if not row.get("kernel_row"):
                continue
            c = row["classify"]
            got = wheel.auto_state_verdict(
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

    def test_flag_on_store_load_matches_golden_loads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _real_kernel()
        monkeypatch.setenv(_FLAG, "1")
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


class TestSeamHelperTotality:
    def test_rust_state_refusal_is_total_for_non_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: 1)
        )
        assert _rust_state_refusal("junk") == "not a JSON object"
        monkeypatch.setitem(
            sys.modules, "brutex_pull_state", _fake_kernel(auto_state_verdict=lambda *a: None)
        )
        assert _rust_state_refusal({"schema": 1, "armed": True}) is None
