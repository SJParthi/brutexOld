"""Unit suite for :mod:`brutex.observability.flight_recorder` (R41/R43, D-7/D-9/D-20).

Pins the AuditWriter 8-key line shape with ``actor="flight"``, the exact
P2→P4 ``contract_view`` superset mapping, the degraded latch (ONE WARN then
silent), the drop counter, lossless TUPLE_DONE decimation with cumulative
counters, the milestone edge gate, and the bounded forensic search.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.flight_recorder import (
    FLIGHT_ACTOR,
    FLIGHT_RECORDER_FILENAME,
    MILESTONE_PCTS,
    RECORD_KINDS,
    FlightRecorder,
    contract_view,
    iter_run_records,
    recorder_has_suspect,
    recorder_path,
    search_timeline,
    verify_recorder_chain,
)
from brutex.orchestrator.audit import (
    AuditEntry,
    AuditWriter,
    verify_chain,
)
from structlog.testing import capture_logs

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r41_record_kinds_vocabulary_pinned": ("R41",),
    "test_r43_recorder_path_law": ("R43",),
    "test_r41_record_line_is_auditwriter_8key_shape_actor_flight": ("R41", "R43"),
    "test_r41_open_resumes_existing_chain": ("R41",),
    "test_r41_open_rejects_bad_decimation_stride": ("R41",),
    "test_r41_open_failure_returns_born_degraded_recorder": ("R41",),
    "test_r41_unknown_kind_dropped_and_warned": ("R41",),
    "test_r41_degraded_latch_one_warn_then_silent": ("R41",),
    "test_r41_payload_defect_drops_without_latch": ("R41",),
    "test_r41_pipe_run_id_field_defect_drops_without_latch": ("R41",),
    "test_r41_tuple_done_decimation_lossless_cumulative": ("R41",),
    "test_r41_decimation_stride_one_records_every_tuple": ("R41",),
    "test_r41_milestone_pct_edge_gated": ("R41",),
    "test_r41_milestone_edge_consumed_even_when_degraded": ("R41",),
    "test_r41_close_idempotent_and_fail_soft": ("R41",),
    "test_r41_contract_view_exact_four_keys": ("R41",),
    "test_r41_iter_run_records_streaming_and_missing_file": ("R41",),
    "test_r41_search_timeline_filters_and_bounds": ("R41",),
    "test_r41_verify_recorder_chain_delegates_to_auto_verify": ("R41",),
}

_RUN_ID: Final[str] = "ab" * 32


def _open(tmp_path: Path, **kwargs: int) -> FlightRecorder:
    return FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1, **kwargs)


def _payload(record: dict[str, object]) -> dict[str, object]:
    """Narrow a contract-view record's payload for typed assertions."""
    payload = record["payload"]
    assert isinstance(payload, dict)
    return payload


class TestVocabulary:
    def test_r41_record_kinds_vocabulary_pinned(self) -> None:
        """The record-kind vocabulary is the exact frozen design-§2 set."""
        assert (
            frozenset(
                {
                    "RUN_START",
                    "STAGE_START",
                    "STAGE_END",
                    "TUPLE_DONE",
                    "TUPLE_FAILED",
                    "TUPLE_SKIPPED",
                    "CHECKPOINT",
                    "QUARANTINE",
                    "REFUSAL",
                    "RETRY",
                    "TRIPWIRE",
                    "MILESTONE",
                    "DIFFCHECK_OK",
                    "DIFFCHECK_MISMATCH",
                    "SUSPECT",
                    "HALT",
                    "RESUME",
                    "MANIFEST_WRITTEN",
                    "BUNDLE_WRITTEN",
                    "RUN_END",
                }
            )
            == RECORD_KINDS
        )
        assert MILESTONE_PCTS == (25, 50, 75)
        assert FLIGHT_ACTOR == "flight"

    def test_r43_recorder_path_law(self, tmp_path: Path) -> None:
        """D-15: the flight file lives at runs/<run_id>/flight-recorder.jsonl."""
        assert (
            recorder_path(tmp_path, _RUN_ID)
            == tmp_path / "runs" / _RUN_ID / FLIGHT_RECORDER_FILENAME
        )
        assert FLIGHT_RECORDER_FILENAME == "flight-recorder.jsonl"


class TestRecord:
    def test_r41_record_line_is_auditwriter_8key_shape_actor_flight(self, tmp_path: Path) -> None:
        """One record = one 8-key AuditWriter line: actor=flight, action=kind,
        subject=run_id, payload stamped with attempt."""
        recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=3)
        assert recorder.record("RUN_START", {"total": 2}) is True
        assert recorder.record("RUN_END", None) is True
        recorder.close()
        lines = recorder_path(tmp_path, _RUN_ID).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert set(first.keys()) == {
            "seq",
            "prev_hash",
            "ts_utc",
            "actor",
            "action",
            "subject",
            "payload",
            "entry_hash",
        }
        assert first["actor"] == "flight"
        assert first["action"] == "RUN_START"
        assert first["subject"] == _RUN_ID
        assert first["payload"] == {"total": 2, "attempt": 3}
        assert first["seq"] == 0
        second = json.loads(lines[1])
        assert second["payload"] == {"attempt": 3}
        assert verify_chain(recorder_path(tmp_path, _RUN_ID)) == 2
        assert recorder.degraded is False
        assert recorder.dropped == 0

    def test_r41_open_resumes_existing_chain(self, tmp_path: Path) -> None:
        """Re-opening resumes the chain (seq continues; whole file verifies)."""
        recorder = _open(tmp_path)
        assert recorder.record("RUN_START", {"total": 1}) is True
        recorder.close()
        resumed = _open(tmp_path)
        assert resumed.record("RESUME", {"from": 1}) is True
        resumed.close()
        path = recorder_path(tmp_path, _RUN_ID)
        assert verify_chain(path) == 2
        entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert [e["seq"] for e in entries] == [0, 1]
        assert entries[1]["prev_hash"] == entries[0]["entry_hash"]

    def test_r41_open_rejects_bad_decimation_stride(self, tmp_path: Path) -> None:
        """tuple_record_every < 1 is a launch-time config defect — fail-loud."""
        with pytest.raises(ValueError, match="tuple_record_every must be >= 1, got 0"):
            _open(tmp_path, tuple_record_every=0)

    def test_r41_open_failure_returns_born_degraded_recorder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An AuditWriter.open failure yields a degraded recorder + ONE open WARN."""

        def _boom(*args: object, **kwargs: object) -> AuditWriter:
            raise OSError("disk on fire")

        monkeypatch.setattr(AuditWriter, "open", _boom)
        with capture_logs() as logs:
            recorder = _open(tmp_path)
            assert recorder.degraded is True
            assert recorder.record("RUN_START", {}) is False
            assert recorder.record("RUN_END", {}) is False
        assert recorder.dropped == 2
        open_warns = [e for e in logs if e["event"] == "flight_recorder.open_failed"]
        assert len(open_warns) == 1
        assert open_warns[0]["error_type"] == "OSError"
        append_warns = [e for e in logs if e["event"] == "flight_recorder.append_failed"]
        assert append_warns == []
        recorder.close()  # writer is None → early return, still safe

    def test_r41_unknown_kind_dropped_and_warned(self, tmp_path: Path) -> None:
        """A kind outside RECORD_KINDS is dropped + counted + WARNed, never appended."""
        recorder = _open(tmp_path)
        with capture_logs() as logs:
            assert recorder.record("TUPLE_DNE", {"typo": True}) is False
        assert recorder.dropped == 1
        assert recorder.degraded is False
        warns = [e for e in logs if e["event"] == "flight_recorder.unknown_kind"]
        assert len(warns) == 1
        assert warns[0]["kind"] == "TUPLE_DNE"
        assert recorder.record("RUN_START", {}) is True  # recorder still healthy
        recorder.close()
        assert verify_chain(recorder_path(tmp_path, _RUN_ID)) == 1

    def test_r41_degraded_latch_one_warn_then_silent(self, tmp_path: Path) -> None:
        """D-20: first append failure → ONE WARN + latch; later drops are silent,
        counted, and never re-attempt the write."""
        recorder = _open(tmp_path)
        assert recorder.record("RUN_START", {}) is True
        writer = recorder._writer  # pyright: ignore[reportPrivateUsage]
        assert writer is not None
        calls = {"n": 0}
        real_append = writer.append

        def _failing_append(**kwargs: object) -> object:
            calls["n"] += 1
            raise OSError("ENOSPC-ish")

        writer.append = _failing_append  # type: ignore[assignment,method-assign]
        with capture_logs() as logs:
            assert recorder.record("CHECKPOINT", {"index": 1}) is False
            assert recorder.record("CHECKPOINT", {"index": 2}) is False
            assert recorder.record("RUN_END", {}) is False
        warns = [e for e in logs if e["event"] == "flight_recorder.append_failed"]
        assert len(warns) == 1  # ONE WARN then silent (D-20)
        assert warns[0]["error_type"] == "OSError"
        assert calls["n"] == 1  # NO retry spam — the latch short-circuits appends
        assert recorder.degraded is True
        assert recorder.dropped == 3
        writer.append = real_append  # type: ignore[method-assign]
        assert recorder.record("RUN_END", {}) is False  # latch never clears in-run
        assert recorder.dropped == 4
        recorder.close()

    def test_r41_payload_defect_drops_without_latch(self, tmp_path: Path) -> None:
        """A NaN payload rejects ONLY that record (WARN, counted) — no latch."""
        recorder = _open(tmp_path)
        with capture_logs() as logs:
            assert recorder.record("TRIPWIRE", {"value": float("nan")}) is False
        warns = [e for e in logs if e["event"] == "flight_recorder.payload_rejected"]
        assert len(warns) == 1
        assert warns[0]["kind"] == "TRIPWIRE"
        assert recorder.degraded is False
        assert recorder.dropped == 1
        assert recorder.record("RUN_END", {}) is True  # disk healthy → still appends
        recorder.close()
        assert verify_chain(recorder_path(tmp_path, _RUN_ID)) == 1

    def test_r41_pipe_run_id_field_defect_drops_without_latch(self, tmp_path: Path) -> None:
        """A '|'-bearing subject (AuditFieldError) is a per-record defect, not a latch."""
        recorder = FlightRecorder(
            writer=AuditWriter.open(recorder_path(tmp_path, "pipe"), actor=FLIGHT_ACTOR),
            run_id="bad|run",
            attempt=1,
            tuple_record_every=1,
        )
        with capture_logs() as logs:
            assert recorder.record("RUN_START", {}) is False
        warns = [e for e in logs if e["event"] == "flight_recorder.payload_rejected"]
        assert len(warns) == 1
        assert recorder.degraded is False
        assert recorder.dropped == 1
        recorder.close()

    def test_r41_tuple_done_decimation_lossless_cumulative(self, tmp_path: Path) -> None:
        """D-9: every=3 records calls 1,4,7…; skips return False WITHOUT counting a
        drop; recorded payloads keep the cumulative counters intact."""
        recorder = _open(tmp_path, tuple_record_every=3)
        results = []
        for i in range(1, 8):
            results.append(
                recorder.record(
                    "TUPLE_DONE",
                    {"index": i - 1, "done_cum": i, "skipped_cum": 0, "failed_cum": 0},
                )
            )
        assert results == [True, False, False, True, False, False, True]
        assert recorder.dropped == 0  # decimation skips are policy, never drops
        assert recorder.degraded is False
        recorder.close()
        records = list(iter_run_records(tmp_path, _RUN_ID))
        assert [r["kind"] for r in records] == ["TUPLE_DONE"] * 3
        payloads = [_payload(r) for r in records]
        assert [p["done_cum"] for p in payloads] == [1, 4, 7]
        assert all(p["skipped_cum"] == 0 and p["failed_cum"] == 0 for p in payloads)

    def test_r41_decimation_stride_one_records_every_tuple(self, tmp_path: Path) -> None:
        """The default stride (None → 1) records every TUPLE_DONE."""
        recorder = _open(tmp_path)
        assert all(recorder.record("TUPLE_DONE", {"done_cum": i}) for i in range(1, 5))
        recorder.close()
        assert verify_recorder_chain(tmp_path, _RUN_ID) == 4


class TestMilestones:
    def test_r41_milestone_pct_edge_gated(self, tmp_path: Path) -> None:
        """Each of 25/50/75 records exactly once; other pcts never record."""
        recorder = _open(tmp_path)
        assert recorder.milestone_pct(30, {"tuples_done": 3}) is False
        assert recorder.milestone_pct(25, {"tuples_done": 25, "total": 100}) is True
        assert recorder.milestone_pct(25, {"tuples_done": 26, "total": 100}) is False
        assert recorder.milestone_pct(50, {"tuples_done": 50, "total": 100}) is True
        recorder.close()
        records = list(iter_run_records(tmp_path, _RUN_ID))
        assert [r["kind"] for r in records] == ["MILESTONE", "MILESTONE"]
        assert records[0]["payload"] == {"pct": 25, "tuples_done": 25, "total": 100, "attempt": 1}
        assert records[1]["payload"] == {"pct": 50, "tuples_done": 50, "total": 100, "attempt": 1}

    def test_r41_milestone_edge_consumed_even_when_degraded(self, tmp_path: Path) -> None:
        """A degraded recorder consumes the edge (no retry spam later — D-20)."""
        recorder = FlightRecorder(writer=None, run_id=_RUN_ID, attempt=1, tuple_record_every=1)
        assert recorder.degraded is True
        assert recorder.milestone_pct(25, {}) is False
        assert recorder.dropped == 1
        assert recorder.milestone_pct(25, {}) is False  # edge already consumed
        assert recorder.dropped == 1  # NOT double-counted: gate fires before append


class TestClose:
    def test_r41_close_idempotent_and_fail_soft(self, tmp_path: Path) -> None:
        """close() twice is safe; a close error is ONE WARN, never a raise."""
        recorder = _open(tmp_path)
        assert recorder.record("RUN_START", {}) is True
        writer = recorder._writer  # pyright: ignore[reportPrivateUsage]
        assert writer is not None

        real_close = writer.close

        def _bad_close() -> None:
            raise OSError("close failed")

        writer.close = _bad_close  # type: ignore[method-assign]
        with capture_logs() as logs:
            recorder.close()
            recorder.close()  # idempotent — writer already detached
        warns = [e for e in logs if e["event"] == "flight_recorder.close_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "OSError"
        real_close()  # release the real fd (test hygiene — no ResourceWarning)


class TestReaders:
    def test_r41_contract_view_exact_four_keys(self) -> None:
        """D-7: contract_view is EXACTLY {ts_utc, run_id, kind, payload} with
        kind=action and run_id=subject."""
        entry = AuditEntry(
            seq=0,
            prev_hash="0" * 64,
            ts_utc=123.5,
            actor=FLIGHT_ACTOR,
            action="STAGE_START",
            subject=_RUN_ID,
            payload={"stage": "sweep", "attempt": 1},
            entry_hash="e" * 64,
        )
        view = contract_view(entry)
        assert view == {
            "ts_utc": 123.5,
            "run_id": _RUN_ID,
            "kind": "STAGE_START",
            "payload": {"stage": "sweep", "attempt": 1},
        }
        assert list(view.keys()) == ["ts_utc", "run_id", "kind", "payload"]

    def test_r41_iter_run_records_streaming_and_missing_file(self, tmp_path: Path) -> None:
        """A missing recorder yields an EMPTY iterator (honest degrade, never a 500);
        a real file streams contract views in chain order."""
        assert list(iter_run_records(tmp_path, _RUN_ID)) == []
        recorder = _open(tmp_path)
        recorder.record("RUN_START", {"total": 1})
        recorder.record("RUN_END", {"exit_code": 0})
        recorder.close()
        records = list(iter_run_records(tmp_path, _RUN_ID))
        assert [r["kind"] for r in records] == ["RUN_START", "RUN_END"]
        assert all(r["run_id"] == _RUN_ID for r in records)

    def test_r41_search_timeline_filters_and_bounds(self, tmp_path: Path) -> None:
        """kind/since/until filters compose; limit bounds; limit<=0 and a missing
        file both return [] (bounded forensics)."""
        assert search_timeline(tmp_path, _RUN_ID) == []  # missing file
        recorder = _open(tmp_path)
        recorder.record("RUN_START", {})
        for i in range(5):
            recorder.record("CHECKPOINT", {"index": i})
        recorder.record("RUN_END", {})
        recorder.close()
        path = recorder_path(tmp_path, _RUN_ID)
        ts = [json.loads(line)["ts_utc"] for line in path.read_text(encoding="utf-8").splitlines()]
        all_records = search_timeline(tmp_path, _RUN_ID)
        assert len(all_records) == 7
        checkpoints = search_timeline(tmp_path, _RUN_ID, kind="CHECKPOINT")
        assert [_payload(r)["index"] for r in checkpoints] == [0, 1, 2, 3, 4]
        bounded = search_timeline(tmp_path, _RUN_ID, kind="CHECKPOINT", limit=2)
        assert [_payload(r)["index"] for r in bounded] == [0, 1]
        assert search_timeline(tmp_path, _RUN_ID, limit=0) == []
        # since/until are INCLUSIVE bounds on ts_utc.
        windowed = search_timeline(tmp_path, _RUN_ID, since=ts[1], until=ts[5])
        assert len(windowed) == 5
        assert search_timeline(tmp_path, _RUN_ID, since=ts[-1] + 1.0) == []
        assert search_timeline(tmp_path, _RUN_ID, until=ts[0] - 1.0) == []

    def test_r41_verify_recorder_chain_delegates_to_auto_verify(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-2: verification goes through brutex.audit_verify.auto_verify (the ONE
        matching verifier) — pinned by spying the delegation AND by entry count."""
        recorder = _open(tmp_path)
        recorder.record("RUN_START", {})
        recorder.record("RUN_END", {})
        recorder.close()
        assert verify_recorder_chain(tmp_path, _RUN_ID) == 2

        import brutex.audit_verify as audit_verify_mod

        seen: list[Path] = []
        real = audit_verify_mod.auto_verify

        def _spy(path: Path) -> object:
            seen.append(path)
            return real(path)

        monkeypatch.setattr(audit_verify_mod, "auto_verify", _spy)
        assert verify_recorder_chain(tmp_path, _RUN_ID) == 2
        assert seen == [recorder_path(tmp_path, _RUN_ID)]


class TestRecorderHasSuspect:
    """The D-12 never-launder belt (MAJOR round 2): a durable SUSPECT record read."""

    def test_true_when_suspect_recorded(self, tmp_path: Path) -> None:
        recorder = _open(tmp_path)
        recorder.record("RUN_START", {})
        recorder.record("SUSPECT", {"reason": "diffcheck_mismatch", "detail": "x"})
        recorder.close()
        assert recorder_has_suspect(tmp_path, _RUN_ID) is True

    def test_false_when_no_suspect(self, tmp_path: Path) -> None:
        recorder = _open(tmp_path)
        recorder.record("RUN_START", {})
        recorder.record("RUN_END", {})
        recorder.close()
        assert recorder_has_suspect(tmp_path, _RUN_ID) is False

    def test_false_when_file_missing(self, tmp_path: Path) -> None:
        assert recorder_has_suspect(tmp_path, _RUN_ID) is False

    def test_tolerates_blank_malformed_and_non_mapping_lines(self, tmp_path: Path) -> None:
        path = recorder_path(tmp_path, _RUN_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n"  # blank line → skipped
            "{not json\n"  # malformed → skipped
            "[1, 2, 3]\n"  # valid JSON but not a Mapping → skipped
            '{"action": "RUN_START"}\n'  # a real non-SUSPECT record
            '{"action": "SUSPECT"}\n',  # the durable SUSPECT
            encoding="utf-8",
        )
        assert recorder_has_suspect(tmp_path, _RUN_ID) is True
