"""ON-arm integration parity for the observability Rust lane (DEC-RUST-OBSERVABILITY-001).

Drives the FOUR REAL public entry points — ``loop_watchdog.check_once``,
``Watchdog.check_once``, ``SLADetector.check_once`` and
``validate_stage_stamp``/``read_stage_stamp`` — with the flag ON, the REAL
built wheel installed, and ``remaining=0`` (post-burn-in steady state), while
the Python ORACLE laws are monkeypatched to BOMBS: the engagement spy. A
correct result with the oracle bombed proves the CRATE served the decision
(never the oracle); the ``observability_rust.engaged`` INFO is the log-side
witness. Plus the real-wheel burn-in lifecycle proof (``burn_in_complete``).

Wheel-GATED: the whole module skips cleanly when ``brutex_observability`` is
not importable (main CI never installs it — the LAKE posture; the dedicated
``observability-rust-parity.yml`` lane is where this file has teeth).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, NoReturn

import pytest
from brutex.observability import loop_watchdog, observability_rust, sla, stage_stamp, watchdog
from brutex.observability.loop_watchdog import WatchdogState
from brutex.observability.sla import SLADetector, SLAEvent, SLAPredicate, SLARule, SLAState
from brutex.observability.stage_stamp import (
    StageStampError,
    StampVerdict,
    read_stage_stamp,
    stamp_path,
    validate_stage_stamp,
    write_stage_stamp,
)
from brutex.observability.watchdog import Watchdog, WorkerEvent, WorkerState
from structlog.testing import capture_logs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

pytest.importorskip("brutex_observability")


def _oracle_bomb(*args: object, **kwargs: object) -> NoReturn:
    """The engagement spy — a routed call must never consult the oracle."""
    raise AssertionError("the flag-ON steady-state path consulted the Python oracle")


@pytest.fixture(autouse=True)
def _flag_on_steady(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Flag ON + post-burn-in steady state around every test; reset after."""
    monkeypatch.setenv(observability_rust.OBSERVABILITY_RUST_ENV, "1")
    observability_rust.reset_observability_rust_for_tests(remaining=0)
    yield
    observability_rust.reset_observability_rust_for_tests()


class TestLoopWatchdogEntry:
    def test_warn_edge_served_by_crate_with_engaged_info(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(10.0, 0.0, WatchdogState())
        assert verdict.warn_edge is True
        assert verdict.critical_edge is False
        assert verdict.lag_s == 10.0
        assert verdict.state.tier == loop_watchdog.TIER_WARNED
        assert verdict.state.stall_started_beat == 0.0
        assert verdict.state.last_logged_at == 10.0
        assert verdict.state.episodes == 1
        engaged = [e for e in logs if e["event"] == "observability_rust.engaged"]
        assert len(engaged) == 1

    def test_ok_to_ok_recomposes_equal_but_not_identical_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The documented flag-ON envelope: EQUAL frozen dataclasses, not the
        # oracle's object identity (flag-OFF preserves identity instead).
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        state = WatchdogState()
        verdict = loop_watchdog.check_once(0.0, 0.0, state)
        assert verdict.state == state
        assert verdict.state is not state
        assert verdict.lag_s == 0.0
        assert verdict.recovered is False

    def test_recovery_totals_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        stalled = WatchdogState(
            tier=loop_watchdog.TIER_CRITICAL,
            stall_started_beat=5.0,
            last_logged_at=40.0,
            last_alerted_at=40.0,
            episodes=3,
        )
        verdict = loop_watchdog.check_once(50.0, 49.0, stalled)
        assert verdict.recovered is True
        assert verdict.stall_total_s == 44.0  # last_beat − stall_started_beat
        assert verdict.state.tier == loop_watchdog.TIER_OK
        assert verdict.state.episodes == 3  # carried across recovery


class TestWatchdogEntry:
    def test_hung_then_recovery_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watchdog, "_worker_decide_python", _oracle_bomb)
        times = [0.0, 20.0, 21.0, 22.0]
        events: list[WorkerEvent] = []
        dog = Watchdog(
            timeout_seconds=15.0,
            on_hung=events.append,
            on_recovery=events.append,
            clock=lambda: times.pop(0),
        )
        dog.register("w")  # t = 0.0
        hung = dog.check_once()  # t = 20.0 → elapsed 20 > 15 → HUNG edge
        assert [e.new_state for e in hung] == [WorkerState.HUNG]
        assert hung[0].last_beat_seconds_ago == 20.0
        assert dog.state_of("w") is WorkerState.HUNG
        dog.beat("w")  # t = 21.0
        recovered = dog.check_once()  # t = 22.0 → elapsed 1 → recovery edge
        assert [e.new_state for e in recovered] == [WorkerState.HEALTHY]
        assert events == [*hung, *recovered]

    def test_exactly_at_timeout_is_not_hung(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(watchdog, "_worker_decide_python", _oracle_bomb)
        times = [0.0, 15.0]
        dog = Watchdog(
            timeout_seconds=15.0,
            on_hung=_oracle_bomb,
            on_recovery=_oracle_bomb,
            clock=lambda: times.pop(0),
        )
        dog.register("w")
        assert dog.check_once() == ()  # strict `>` — the boundary tie is healthy
        assert dog.state_of("w") is WorkerState.HEALTHY


class TestSlaEntry:
    def test_breach_then_recovery_served_by_crate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sla, "_sla_decide_python", _oracle_bomb)
        values = [150.0, 50.0]
        events: list[SLAEvent] = []
        detector = SLADetector(
            rules=(
                SLARule(
                    name="rate",
                    value_provider=lambda: values.pop(0),
                    predicate=SLAPredicate.GREATER_THAN,
                    threshold=100.0,
                ),
            ),
            on_breach=events.append,
            on_recovery=events.append,
        )
        breach = detector.check_once()
        assert [e.new_state for e in breach] == [SLAState.BREACH]
        assert breach[0].observed_value == 150.0  # the ORIGINAL value object
        assert detector.state_of("rate") is SLAState.BREACH
        recovery = detector.check_once()
        assert [e.new_state for e in recovery] == [SLAState.OK]
        assert events == [*breach, *recovery]


class TestStampEntry:
    @staticmethod
    def _bomb_oracles(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(stage_stamp, "_stamp_shape_index_python", _oracle_bomb)
        monkeypatch.setattr(stage_stamp, "_stamp_verdict_python", _oracle_bomb)

    def test_validate_ok_and_hash_mismatch_served_by_crate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bomb_oracles(monkeypatch)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        write_stage_stamp(
            artifact,
            stage="cascade_manifest",
            run_id="r1",
            extra={"data_snapshot_hash": "snap"},
        )
        verdict, stamp = validate_stage_stamp(
            artifact,
            expected_stage="cascade_manifest",
            expected_run_id="r1",
            expected_snapshot_hash="snap",
        )
        assert verdict is StampVerdict.OK  # the enum SINGLETON identity law
        assert stamp is not None and stamp.run_id == "r1"
        artifact.write_bytes(b"tampered")  # torn artifact → recompute mismatch
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_manifest")
        assert verdict is StampVerdict.HASH_MISMATCH
        assert stamp is not None  # forensics stamp returned

    def test_malformed_stamp_message_minted_from_crate_index(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._bomb_oracles(monkeypatch)
        artifact = tmp_path / "artifact.bin"
        artifact.write_bytes(b"payload")
        stamp_path(artifact).write_text(
            json.dumps(
                {
                    "stage": 5,
                    "run_id": "r1",
                    "content_hash": "h",
                    "produced_at_utc": 1.0,
                    "extra": {},
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(StageStampError) as excinfo:
            read_stage_stamp(artifact)
        assert "field 'stage' must be a str, got 5" in str(excinfo.value)
        # validate folds the SAME crate-served reject into MALFORMED.
        verdict, stamp = validate_stage_stamp(artifact, expected_stage="cascade_manifest")
        assert verdict is StampVerdict.MALFORMED
        assert stamp is None


class TestOverflowContainmentEntry:
    def test_u64_max_episodes_degrades_to_oracle_through_public_check_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The overflow-containment law at the PUBLIC surface: episodes ==
        # u64::MAX is outside the crate's counter range (its checked
        # ``episodes + 1`` on a warn edge cannot represent the result), so
        # the facade's PRE-FFI range gate routes it to the ORACLE, which
        # serves the unbounded-int truth. The crate is never called at all —
        # strictly stronger than the earlier shape, where the call crossed
        # the FFI and came back as a ``loop_error`` degrade (and, before
        # THAT, as the wheel's per-extension PanicException, a BaseException
        # NEITHER name-based belt home could resolve). The oracle is
        # deliberately NOT bombed here — the CLASS-C arm legitimately
        # consults it, and does so even under BRUTEX_RUST_STRICT (this file
        # runs at the strict default, so a green run IS that proof).
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(10.0, 0.0, WatchdogState(episodes=2**64 - 1))
        assert verdict.warn_edge is True
        assert verdict.state.episodes == 2**64  # Python ints are unbounded
        warns = [e for e in logs if e["event"] == "observability_rust.fallback"]
        assert warns[0]["reason"] == "loop_input_beyond_crate"
        assert warns[0]["episodes"] == 2**64 - 1
        assert observability_rust.rust_observability_disabled() is False
        # NOT latched: the very next in-domain call is crate-served again.
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        assert loop_watchdog.check_once(0.0, 0.0, WatchdogState()).lag_s == 0.0


class TestBurnInLifecycleOnRealWheel:
    def test_burn_in_completes_then_steady(self, monkeypatch: pytest.MonkeyPatch) -> None:
        observability_rust.reset_observability_rust_for_tests(remaining=1)
        with capture_logs() as logs:
            verdict = loop_watchdog.check_once(0.0, 0.0, WatchdogState())
        assert verdict.lag_s == 0.0
        done = [e for e in logs if e["event"] == "observability_rust.burn_in_complete"]
        assert [e["law"] for e in done] == ["loop"]
        assert observability_rust.rust_observability_disabled() is False
        # Steady thereafter: the oracle is out of the path entirely.
        monkeypatch.setattr(loop_watchdog, "_check_once_python", _oracle_bomb)
        assert loop_watchdog.check_once(0.0, 0.0, WatchdogState()).lag_s == 0.0
