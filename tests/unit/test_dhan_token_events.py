"""Unit tests for :mod:`brutex.adapters.dhan.token_events` (DEC-TOKEN-EVENTS-001).

100% line+branch on the token-lifecycle event log: client-id masking, the
token-FREE append, never-raise load degradation, the JSON projection, and the
load-bearing SECURITY assertion — the raw token / full client id NEVER reach the
on-disk bytes or the /db projection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from brutex.adapters.dhan.token_events import (
    TokenEvent,
    events_dir,
    events_path,
    load_token_events,
    mask_client_id,
    record_token_event,
    to_payload,
)

_NOW = datetime(2026, 6, 8, 12, 0, tzinfo=UTC)
_EXP = datetime(2026, 6, 9, 12, 0, tzinfo=UTC)


def test_paths(tmp_path: Path) -> None:
    assert events_dir(tmp_path) == tmp_path / "token_events"
    assert events_path(tmp_path) == tmp_path / "token_events" / "events.parquet"


class TestMaskClientId:
    def test_none_and_empty(self) -> None:
        assert mask_client_id(None) is None
        assert mask_client_id("") is None

    def test_short_id_fully_starred(self) -> None:
        assert mask_client_id("123") == "***"
        assert mask_client_id("12345678") == "********"  # exactly 8 → all starred

    def test_long_id_keeps_first_last_four(self) -> None:
        assert mask_client_id("1100123456788842") == "1100********8842"


class TestRecordAndLoad:
    def test_append_one_event(self, tmp_path: Path) -> None:
        record_token_event(
            tmp_path,
            action="saved",
            status="ok",
            expiry_at=_EXP,
            client_id="1100123456788842",
            reason=None,
            now=_NOW,
        )
        assert events_path(tmp_path).is_file()
        (event,) = load_token_events(tmp_path)
        assert event.action == "saved"
        assert event.status == "ok"
        assert event.event_at == _NOW.isoformat()
        assert event.expiry_at == _EXP.isoformat()
        assert event.client_id_masked == "1100********8842"  # masked, not the full id

    def test_appends_accumulate(self, tmp_path: Path) -> None:
        record_token_event(tmp_path, action="saved", now=_NOW)
        record_token_event(tmp_path, action="renewed", expiry_at=_EXP, now=_NOW)
        record_token_event(tmp_path, action="refused", status="failed", reason="DH-905", now=_NOW)
        events = load_token_events(tmp_path)
        assert [e.action for e in events] == ["saved", "renewed", "refused"]
        assert events[1].expiry_at == _EXP.isoformat()
        assert events[2].status == "failed"
        assert events[2].reason == "DH-905"

    def test_record_defaults_now(self, tmp_path: Path) -> None:
        record_token_event(tmp_path, action="renewed")  # now=None → datetime.now(UTC)
        (event,) = load_token_events(tmp_path)
        assert event.action == "renewed"
        assert event.expiry_at is None  # no expiry passed
        assert event.client_id_masked is None  # no client id passed

    def test_load_missing_is_empty(self, tmp_path: Path) -> None:
        assert load_token_events(tmp_path) == []

    def test_load_corrupt_degrades_to_empty(self, tmp_path: Path) -> None:
        path = events_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet file")
        assert load_token_events(tmp_path) == []  # WARNING, never raises

    def test_load_wrong_shape_degrades_to_empty(self, tmp_path: Path) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = events_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.table({"unexpected": ["x"]}), path)  # missing the event columns
        assert load_token_events(tmp_path) == []


def test_to_payload() -> None:
    events = [
        TokenEvent(
            event_at="2026-06-08T12:00:00+00:00",
            action="saved",
            status="ok",
            expiry_at="2026-06-09T12:00:00+00:00",
            client_id_masked="1100********8842",
            reason=None,
        )
    ]
    rows = to_payload(events)
    assert rows == [
        {
            "event_at": "2026-06-08T12:00:00+00:00",
            "action": "saved",
            "status": "ok",
            "expiry_at": "2026-06-09T12:00:00+00:00",
            "client_id_masked": "1100********8842",
            "reason": None,
        }
    ]


class TestSecurityBoundary:
    """The load-bearing gate: the token / full client id NEVER leak to the table."""

    def test_no_token_column_exists(self, tmp_path: Path) -> None:
        record_token_event(tmp_path, action="saved", client_id="1100123456788842", now=_NOW)
        (event,) = load_token_events(tmp_path)
        # The dataclass has NO field that could hold the token secret.
        fields = set(event.__slots__)  # type: ignore[attr-defined]
        assert "token" not in fields
        assert "access_token" not in fields
        # The /db projection is the same fixed allowlist — no token key.
        (row,) = to_payload([event])
        assert set(row) == {
            "event_at",
            "action",
            "status",
            "expiry_at",
            "client_id_masked",
            "reason",
        }

    def test_full_client_id_never_hits_the_bytes(self, tmp_path: Path) -> None:
        full = "1100123456788842"
        record_token_event(tmp_path, action="saved", client_id=full, now=_NOW)
        raw = events_path(tmp_path).read_bytes()
        assert full.encode() not in raw  # the full id is masked before it is written
        assert b"1100********8842" in raw  # only the masked form is stored
