"""Byte-frozen golden for the flight-recorder line format (R41, D-2/D-7).

Freezes the EXACT on-disk bytes a :class:`FlightRecorder` produces for a
3-record run under a pinned wall clock — the 8-key SHA-256 AuditWriter line
with ``actor="flight"``, canonical JSON, and the frozen entry hashes. Any
drift in the chain preimage, the canonical serialiser, the attempt stamping,
or the actor/subject mapping breaks this file BYTE-for-byte.

The prefill-equivalence pin proves the hand-built
:func:`tests.fakes.audit_chain.chained_entry` fixture writes bit-identical
bytes to the production recorder for identical fields — the same
writer-equivalence law the audit O(1) benches rely on.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Final

import pytest
from brutex.observability.flight_recorder import (
    FLIGHT_ACTOR,
    FlightRecorder,
    contract_view,
    recorder_path,
    verify_recorder_chain,
)
from brutex.orchestrator.audit import AuditEntry, iter_entries

pytestmark = pytest.mark.golden

#: D-6 mechanized traceability — test name → requirement/gap tokens.
TRACEABILITY: Final[dict[str, tuple[str, ...]]] = {
    "test_r41_flight_recorder_bytes_frozen": ("R41", "R43"),
    "test_r41_contract_view_of_frozen_line": ("R41",),
    "test_r41_fixture_entries_are_writer_equivalent": ("R41",),
}

_RUN_ID: Final[str] = "ab" * 32
_TS: Final[float] = 1_700_000_000.0

#: The FROZEN on-disk bytes (generated once from the real recorder under the
#: pinned clock; regenerating requires a ledgered decision — this IS the
#: byte contract).
_GOLDEN_LINES: Final[tuple[str, ...]] = (
    '{"action":"RUN_START","actor":"flight","entry_hash":"4621da408b2beb72f500e407bc942d60085'
    'dd5d01fb9c84dca6d2e518b81f963","payload":{"attempt":1,"total":3,"workers":2},"prev_hash"'
    ':"0000000000000000000000000000000000000000000000000000000000000000","seq":0,"subject":"a'
    'bababababababababababababababababababababababababababababababab","ts_utc":1700000000.0}',
    '{"action":"TUPLE_DONE","actor":"flight","entry_hash":"1cb1fedfae685b8d375e1ea09f142f0c02'
    'c63851928a9e53039d699f8cecfbd4","payload":{"attempt":1,"digest":"d0","done_cum":1,"evalu'
    'ated":10,"failed_cum":0,"index":0,"kept":2,"skipped_cum":0},"prev_hash":"4621da408b2beb7'
    '2f500e407bc942d60085dd5d01fb9c84dca6d2e518b81f963","seq":1,"subject":"ababababababababab'
    'ababababababababababababababababababababababab","ts_utc":1700000000.0}',
    '{"action":"RUN_END","actor":"flight","entry_hash":"92e4e4eb072af9ec3baad4c0331bf9ecd5a6a'
    '1fc661e30ec6acb63e30936d736","payload":{"attempt":1,"exit_code":0,"suspect":false},"prev'
    '_hash":"1cb1fedfae685b8d375e1ea09f142f0c02c63851928a9e53039d699f8cecfbd4","seq":2,"subje'
    'ct":"abababababababababababababababababababababababababababababababab","ts_utc":17000000'
    "00.0}",
)


def _record_golden_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Drive the REAL recorder under the pinned wall clock."""
    import brutex.orchestrator.audit as audit_mod

    monkeypatch.setattr(audit_mod, "time", lambda: _TS)
    recorder = FlightRecorder.open(tmp_path, run_id=_RUN_ID, attempt=1)
    assert recorder.record("RUN_START", {"total": 3, "workers": 2}) is True
    assert (
        recorder.record(
            "TUPLE_DONE",
            {
                "index": 0,
                "digest": "d0",
                "evaluated": 10,
                "kept": 2,
                "done_cum": 1,
                "skipped_cum": 0,
                "failed_cum": 0,
            },
        )
        is True
    )
    assert recorder.record("RUN_END", {"exit_code": 0, "suspect": False}) is True
    recorder.close()
    return recorder_path(tmp_path, _RUN_ID)


class TestFlightRecorderGolden:
    def test_r41_flight_recorder_bytes_frozen(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The recorder reproduces the frozen bytes EXACTLY and the chain verifies."""
        path = _record_golden_run(tmp_path, monkeypatch)
        expected = "".join(line + "\n" for line in _GOLDEN_LINES)
        assert path.read_text(encoding="utf-8") == expected
        assert verify_recorder_chain(tmp_path, _RUN_ID) == 3

    def test_r41_contract_view_of_frozen_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D-7 superset mapping over the FROZEN lines: {ts_utc, run_id, kind,
        payload} == {ts_utc, subject, action, payload}."""
        path = _record_golden_run(tmp_path, monkeypatch)
        views = [contract_view(entry) for entry in iter_entries(path)]
        assert views[0] == {
            "ts_utc": _TS,
            "run_id": _RUN_ID,
            "kind": "RUN_START",
            "payload": {"attempt": 1, "total": 3, "workers": 2},
        }
        assert views[1]["kind"] == "TUPLE_DONE"
        assert views[1]["payload"] == {
            "attempt": 1,
            "index": 0,
            "digest": "d0",
            "evaluated": 10,
            "kept": 2,
            "done_cum": 1,
            "skipped_cum": 0,
            "failed_cum": 0,
        }
        assert views[2] == {
            "ts_utc": _TS,
            "run_id": _RUN_ID,
            "kind": "RUN_END",
            "payload": {"attempt": 1, "exit_code": 0, "suspect": False},
        }

    def test_r41_fixture_entries_are_writer_equivalent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hand-built AuditEntry (the tests/fakes/audit_chain construction
        pattern) with identical fields serialises to the recorder's EXACT bytes."""
        path = _record_golden_run(tmp_path, monkeypatch)
        golden_first = path.read_text(encoding="utf-8").splitlines()[0]
        bare = AuditEntry(
            seq=0,
            prev_hash="0" * 64,
            ts_utc=_TS,
            actor=FLIGHT_ACTOR,
            action="RUN_START",
            subject=_RUN_ID,
            payload={"attempt": 1, "total": 3, "workers": 2},
        )
        hand_built = replace(bare, entry_hash=bare.compute_hash())
        assert hand_built.to_json_line() == golden_first == _GOLDEN_LINES[0]
