"""Unit gate for :mod:`brutex.data.frame_write_gate` (DEC-PREBUILD-FRAME-WRITE-001).

The rust_metrics-shaped control leaf of the frame-direct derived-TF write:
flag resolution (tristate + ``shadow`` + the kill-must-kill junk fallback),
burn-in budgets + the autorevert latch (first-writer-wins, clamped, thread
race benign), the CLOSED degrade taxonomy + its observability policy, the
per-builder stats collector, and the reset seams. 100% line+branch on its own
ci.yml stanza.
"""

from __future__ import annotations

import threading

import pytest
from brutex.data import frame_write_gate as gate
from brutex.data.frame_write_gate import (
    FRESH_SELF_CHECK_SAMPLES,
    GATE_VERDICT_REASONS,
    MERGE_SELF_CHECK_SAMPLES,
    NORMAL_DEGRADE_REASONS,
    PREBUILD_FRAME_WRITE_DEFAULT,
    PREBUILD_FRAME_WRITE_ENV,
    SHADOW_LITERAL,
    DegradeReason,
    FrameWriteMode,
    FrameWriteStats,
)
from structlog.testing import capture_logs


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test sees a fresh-process gate view + an unset flag."""
    monkeypatch.delenv(PREBUILD_FRAME_WRITE_ENV, raising=False)
    gate.reset_gate_for_tests()
    gate.reset_observability_memo_for_tests()


class TestResolveFrameWriteMode:
    """The tristate + shadow + kill-must-kill resolution law."""

    def test_default_is_off_in_pr1(self) -> None:
        """PR-1 ships DARK — the one-constant flip lever is False."""
        assert PREBUILD_FRAME_WRITE_DEFAULT is False
        assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", " ON "])
    def test_truthy_is_on(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, raw)
        assert gate.resolve_frame_write_mode() is FrameWriteMode.ON

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", " Off "])
    def test_falsy_is_off_silent(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, raw)
        with capture_logs() as logs:
            assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF
        assert logs == []  # a RECOGNISED off is silent — only junk warns

    @pytest.mark.parametrize("raw", ["shadow", "SHADOW", "  Shadow  "])
    def test_shadow_literal(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, raw)
        assert gate.resolve_frame_write_mode() is FrameWriteMode.SHADOW
        assert SHADOW_LITERAL == "shadow"

    def test_blank_takes_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "   ")
        assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF

    @pytest.mark.parametrize("raw", ["flase", "0ff", "2", "enable", "shadoww"])
    def test_junk_fails_safe_off_with_one_warn(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        """Kill-must-kill: junk ⇒ OFF + ONE warn-once flag_unrecognised event."""
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, raw)
        with capture_logs() as logs:
            assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF
            assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF
        warns = [e for e in logs if e["log_level"] == "warning"]
        assert len(warns) == 1
        assert warns[0]["event"] == "prebuild.frame_write.degraded"
        assert warns[0]["reason"] == "flag_unrecognised"
        assert warns[0]["value"] == raw
        # The repeat resolution stays visible at DEBUG, never a second WARN.
        debugs = [e for e in logs if e["log_level"] == "debug"]
        assert len(debugs) == 1

    def test_default_on_simulation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The PR-2 flip lever: default True + unset env ⇒ ON (one constant)."""
        monkeypatch.setattr(gate, "PREBUILD_FRAME_WRITE_DEFAULT", True)
        assert gate.resolve_frame_write_mode() is FrameWriteMode.ON
        # The explicit falsy kill switch still KILLS under default-ON.
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "0")
        assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF
        # ... and junk ALSO kills (fail-safe OFF, warned once).
        monkeypatch.setenv(PREBUILD_FRAME_WRITE_ENV, "0ff")
        with capture_logs() as logs:
            assert gate.resolve_frame_write_mode() is FrameWriteMode.OFF
        assert any(e.get("reason") == "flag_unrecognised" for e in logs)


class TestBurnInBudgets:
    """Per-lane budgets: pending → consume → complete-INFO → steady state."""

    def test_fresh_budget_counts_down_and_completes(self) -> None:
        assert gate.fresh_self_check_pending()
        with capture_logs() as logs:
            for _ in range(FRESH_SELF_CHECK_SAMPLES):
                gate.consume_fresh_self_check_sample()
        assert not gate.fresh_self_check_pending()
        completes = [e for e in logs if e["event"] == "prebuild.frame_write.burn_in_complete"]
        assert len(completes) == 1
        assert completes[0]["lane"] == "fresh"

    def test_merge_budget_counts_down_and_completes(self) -> None:
        assert gate.merge_self_check_pending()
        with capture_logs() as logs:
            for _ in range(MERGE_SELF_CHECK_SAMPLES):
                gate.consume_merge_self_check_sample()
        assert not gate.merge_self_check_pending()
        completes = [e for e in logs if e["event"] == "prebuild.frame_write.burn_in_complete"]
        assert len(completes) == 1
        assert completes[0]["lane"] == "merge"

    def test_consume_clamps_at_zero(self) -> None:
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
        gate.consume_fresh_self_check_sample()  # no underflow, no raise
        gate.consume_merge_self_check_sample()
        assert not gate.fresh_self_check_pending()
        assert not gate.merge_self_check_pending()

    def test_lanes_are_independent(self) -> None:
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=2)
        assert not gate.fresh_self_check_pending()
        assert gate.merge_self_check_pending()

    def test_racing_consumers_never_go_negative(self) -> None:
        """The clamped decrement under a real thread race."""
        gate.reset_gate_for_tests(fresh_remaining=4)
        barrier = threading.Barrier(8)

        def hammer() -> None:
            barrier.wait()
            for _ in range(10):
                gate.consume_fresh_self_check_sample()

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not gate.fresh_self_check_pending()
        assert gate._FRESH_REMAINING[0] == 0  # never negative


class TestAutorevertLatch:
    """First-writer-wins, ERROR never throttled, durable for the process."""

    def test_latch_sets_once_and_logs_error(self) -> None:
        assert not gate.autoreverted()
        with capture_logs() as logs:
            gate.autorevert("burn_in_mismatch", partition="p1", lane="fresh")
            gate.autorevert("second_reason", partition="p2", lane="merge")  # no-op
        assert gate.autoreverted()
        errors = [e for e in logs if e["log_level"] == "error"]
        assert len(errors) == 1
        assert errors[0]["event"] == "prebuild.frame_write.autoreverted"
        assert errors[0]["reason"] == "burn_in_mismatch"
        assert errors[0]["partition"] == "p1"

    def test_latch_race_first_writer_wins(self) -> None:
        barrier = threading.Barrier(8)

        def latch(i: int) -> None:
            barrier.wait()
            gate.autorevert(f"reason_{i}")

        threads = [threading.Thread(target=latch, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert gate.autoreverted()
        assert gate._AUTOREVERT_REASON[0] in {f"reason_{i}" for i in range(8)}

    def test_reset_clears_the_latch(self) -> None:
        gate.autorevert("x")
        gate.reset_gate_for_tests()
        assert not gate.autoreverted()


class TestDegradeTaxonomy:
    """The closed enum + the reason → observability policy mapping."""

    def test_taxonomy_is_exactly_the_fifteen_reasons(self) -> None:
        """The CLOSED enum (DESIGN §4.2) — adding a guard must extend it HERE."""
        assert {r.value for r in DegradeReason} == {
            "disabled",
            "autoreverted",
            "flag_unrecognised",
            "schema_mismatch",
            "dup_or_unsorted_ts",
            "domain_violation",
            "magnitude_overflow",
            "snap_overflow",
            "merge_stored_snap",
            "merge_stored_dups",
            "merge_overlay",
            "merge_stored_schema",
            "merge_delta",
            "fast_lane_error",
            "shadow_mismatch",
        }

    def test_policy_sets_partition_the_taxonomy(self) -> None:
        """Every member is a gate verdict, NORMAL, or an anomaly — no overlap."""
        anomalies = set(DegradeReason) - GATE_VERDICT_REASONS - NORMAL_DEGRADE_REASONS
        assert not (GATE_VERDICT_REASONS & NORMAL_DEGRADE_REASONS)
        assert DegradeReason.MERGE_DELTA in NORMAL_DEGRADE_REASONS
        assert DegradeReason.DISABLED in GATE_VERDICT_REASONS
        assert DegradeReason.SCHEMA_MISMATCH in anomalies
        assert DegradeReason.FAST_LANE_ERROR in anomalies

    def test_note_degrade_counts_and_warns_once_for_anomalies(self) -> None:
        stats = FrameWriteStats()
        with capture_logs() as logs:
            gate.note_degrade(DegradeReason.SCHEMA_MISMATCH, stats, partition="p1")
            gate.note_degrade(DegradeReason.SCHEMA_MISMATCH, stats, partition="p2")
        assert stats.degraded == 2
        assert stats.degrade_reasons == {"schema_mismatch": 2}
        warns = [e for e in logs if e["log_level"] == "warning"]
        assert len(warns) == 1  # warn-once per reason; the repeat is DEBUG
        assert warns[0]["reason"] == "schema_mismatch"
        assert [e["log_level"] for e in logs].count("debug") == 1

    def test_note_degrade_merge_delta_is_debug_only(self) -> None:
        """The NORMAL daily shape: DEBUG per partition + counter, never a WARN."""
        stats = FrameWriteStats()
        with capture_logs() as logs:
            gate.note_degrade(DegradeReason.MERGE_DELTA, stats, partition="p")
            gate.note_degrade(DegradeReason.MERGE_DELTA, stats, partition="p")
        assert stats.degrade_reasons == {"merge_delta": 2}
        assert all(e["log_level"] == "debug" for e in logs)
        assert len(logs) == 2

    def test_note_degrade_gate_verdicts_never_counted(self) -> None:
        stats = FrameWriteStats()
        with capture_logs() as logs:
            gate.note_degrade(DegradeReason.DISABLED, stats)
            gate.note_degrade(DegradeReason.SHADOW_MISMATCH, stats)
        assert stats.degraded == 0
        assert stats.degrade_reasons == {}
        assert logs == []  # disabled/shadow-mismatch verdicts log elsewhere

    def test_note_degrade_autoreverted_warns_once(self) -> None:
        """The latch fallback is visible (warn-once) but never counted."""
        stats = FrameWriteStats()
        with capture_logs() as logs:
            gate.note_degrade(DegradeReason.AUTOREVERTED, stats)
            gate.note_degrade(DegradeReason.AUTOREVERTED, stats)
        assert stats.degraded == 0
        warns = [e for e in logs if e["log_level"] == "warning"]
        assert len(warns) == 1
        assert warns[0]["reason"] == "autoreverted"

    def test_note_degrade_without_stats_still_logs(self) -> None:
        with capture_logs() as logs:
            gate.note_degrade(DegradeReason.DOMAIN_VIOLATION, None, partition="p")
        assert len(logs) == 1
        assert logs[0]["reason"] == "domain_violation"


class TestObservabilityHelpers:
    """engaged / burn-in-complete / shadow-mismatch / warn-once mechanics."""

    def test_note_engaged_once_per_process(self) -> None:
        with capture_logs() as logs:
            gate.note_engaged(FrameWriteMode.ON)
            gate.note_engaged(FrameWriteMode.ON)
        infos = [e for e in logs if e["event"] == "prebuild.frame_write.engaged"]
        assert len(infos) == 1
        assert infos[0]["mode"] == "on"
        assert infos[0]["fresh_self_check_samples"] == FRESH_SELF_CHECK_SAMPLES
        assert infos[0]["merge_self_check_samples"] == MERGE_SELF_CHECK_SAMPLES

    def test_shadow_mismatch_error_never_throttled(self) -> None:
        with capture_logs() as logs:
            gate.note_shadow_mismatch(partition="p1")
            gate.note_shadow_mismatch(partition="p2")
        errors = [e for e in logs if e["log_level"] == "error"]
        assert len(errors) == 2  # NEVER throttled
        assert all(e["event"] == "prebuild.frame_write.shadow_mismatch" for e in errors)

    def test_warn_once_key_isolation(self) -> None:
        with capture_logs() as logs:
            gate.warn_once("k1", "some.event", a=1)
            gate.warn_once("k2", "some.event", a=2)
            gate.warn_once("k1", "some.event", a=3)
        warns = [e for e in logs if e["log_level"] == "warning"]
        assert len(warns) == 2  # one per KEY
        assert [e["log_level"] for e in logs].count("debug") == 1

    def test_info_once_suppresses_repeats_entirely(self) -> None:
        with capture_logs() as logs:
            gate.info_once("k", "an.event")
            gate.info_once("k", "an.event")
        assert len(logs) == 1


class TestFrameWriteStats:
    """The per-builder counter object."""

    def test_defaults(self) -> None:
        stats = FrameWriteStats()
        assert stats.fast == 0
        assert stats.degraded == 0
        assert stats.degrade_reasons == {}
        assert stats.shadow_checked == 0
        assert stats.shadow_mismatches == 0

    def test_count_degrade_accumulates(self) -> None:
        stats = FrameWriteStats()
        stats.count_degrade(DegradeReason.MERGE_DELTA)
        stats.count_degrade(DegradeReason.MERGE_DELTA)
        stats.count_degrade(DegradeReason.SNAP_OVERFLOW)
        assert stats.degraded == 3
        assert stats.degrade_reasons == {"merge_delta": 2, "snap_overflow": 1}

    def test_instances_are_independent(self) -> None:
        a, b = FrameWriteStats(), FrameWriteStats()
        a.count_degrade(DegradeReason.MERGE_DELTA)
        assert b.degraded == 0
        assert b.degrade_reasons == {}


class TestResetSeams:
    def test_reset_gate_restores_fresh_process_view(self) -> None:
        gate.reset_gate_for_tests(fresh_remaining=0, merge_remaining=0)
        gate.autorevert("x")
        gate.reset_gate_for_tests()
        assert gate.fresh_self_check_pending()
        assert gate.merge_self_check_pending()
        assert not gate.autoreverted()

    def test_reset_memo_reopens_warn_once(self) -> None:
        with capture_logs() as logs:
            gate.warn_once("k", "e")
            gate.reset_observability_memo_for_tests()
            gate.warn_once("k", "e")
        warns = [e for e in logs if e["log_level"] == "warning"]
        assert len(warns) == 2
