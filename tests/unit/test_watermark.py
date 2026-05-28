"""Tests for `brutex.data.watermark` — the per-chunk fetch-status tracker.

Covers the G-10 anti-loop guard (NO_DATA windows stay settled), resume
semantics (needs_fetch / pending), quarantine of terminal failures, the
status summary, and serialize/restore roundtrip.
"""

from __future__ import annotations

import pytest
from brutex.data.watermark import ChunkKey, ChunkStatus, FetchWatermark


def _key(symbol: str = "NSE-NIFTY", interval: str = "1m") -> ChunkKey:
    return ChunkKey(
        groww_symbol=symbol,
        interval=interval,
        start="2021-01-01T09:15:00",
        end="2021-01-31T15:30:00",
    )


class TestChunkKey:
    def test_is_frozen_hashable(self) -> None:
        k = _key()
        assert hash(k) == hash(_key())
        with pytest.raises(Exception):  # FrozenInstanceError
            k.groww_symbol = "X"  # type: ignore[misc]

    def test_distinct_windows_differ(self) -> None:
        assert _key(symbol="NSE-NIFTY") != _key(symbol="NSE-BANKNIFTY")


class TestRecordGet:
    def test_get_none_when_unrecorded(self) -> None:
        assert FetchWatermark().get(_key()) is None

    def test_record_then_get(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.COMPLETED)
        assert wm.get(_key()) == ChunkStatus.COMPLETED

    def test_record_overwrites(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.PARTIAL)
        wm.record(_key(), ChunkStatus.COMPLETED)
        assert wm.get(_key()) == ChunkStatus.COMPLETED


class TestIsSettled:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ChunkStatus.COMPLETED, True),
            (ChunkStatus.NO_DATA, True),  # the G-10 fix: empty window stays settled
            (ChunkStatus.PARTIAL, False),
            (ChunkStatus.FAILED, False),
        ],
    )
    def test_settled_by_status(self, status: ChunkStatus, expected: bool) -> None:
        wm = FetchWatermark()
        wm.record(_key(), status)
        assert wm.is_settled(_key()) is expected

    def test_unrecorded_is_not_settled(self) -> None:
        assert FetchWatermark().is_settled(_key()) is False


class TestNeedsFetch:
    def test_unrecorded_needs_fetch(self) -> None:
        # None branch of the `or`
        assert FetchWatermark().needs_fetch(_key()) is True

    def test_partial_needs_fetch(self) -> None:
        # second operand of the `or` (recorded, == PARTIAL)
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.PARTIAL)
        assert wm.needs_fetch(_key()) is True

    @pytest.mark.parametrize(
        "status",
        [ChunkStatus.COMPLETED, ChunkStatus.NO_DATA, ChunkStatus.FAILED],
    )
    def test_settled_or_failed_does_not_need_fetch(self, status: ChunkStatus) -> None:
        wm = FetchWatermark()
        wm.record(_key(), status)
        assert wm.needs_fetch(_key()) is False


class TestPending:
    def test_pending_filters_done_and_keeps_partial_and_new(self) -> None:
        done = _key(symbol="NSE-NIFTY")
        empty = _key(symbol="NSE-BANKNIFTY")
        partial = _key(symbol="BSE-SENSEX")
        fresh = _key(symbol="NSE-INDIAVIX")
        failed = _key(symbol="NSE-RELIANCE")
        wm = FetchWatermark()
        wm.record(done, ChunkStatus.COMPLETED)
        wm.record(empty, ChunkStatus.NO_DATA)
        wm.record(partial, ChunkStatus.PARTIAL)
        wm.record(failed, ChunkStatus.FAILED)

        pending = wm.pending([done, empty, partial, fresh, failed])

        assert pending == [partial, fresh]  # done/empty settled; failed quarantined


class TestQuarantined:
    def test_returns_only_failed(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(symbol="A"), ChunkStatus.FAILED)
        wm.record(_key(symbol="B"), ChunkStatus.COMPLETED)
        wm.record(_key(symbol="C"), ChunkStatus.FAILED)
        q = wm.quarantined()
        assert {k.groww_symbol for k in q} == {"A", "C"}

    def test_empty_when_none_failed(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.COMPLETED)
        assert wm.quarantined() == []


class TestSummary:
    def test_empty_is_all_zero(self) -> None:
        assert FetchWatermark().summary() == {
            "completed": 0,
            "no_data": 0,
            "partial": 0,
            "failed": 0,
        }

    def test_counts_by_status(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(symbol="A"), ChunkStatus.COMPLETED)
        wm.record(_key(symbol="B"), ChunkStatus.COMPLETED)
        wm.record(_key(symbol="C"), ChunkStatus.NO_DATA)
        wm.record(_key(symbol="D"), ChunkStatus.FAILED)
        assert wm.summary() == {
            "completed": 2,
            "no_data": 1,
            "partial": 0,
            "failed": 1,
        }


class TestSerializeRestore:
    def test_roundtrip_preserves_all_state(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(symbol="A"), ChunkStatus.COMPLETED)
        wm.record(_key(symbol="B"), ChunkStatus.NO_DATA)
        wm.record(_key(symbol="C"), ChunkStatus.PARTIAL)
        wm.record(_key(symbol="D"), ChunkStatus.FAILED)

        restored = FetchWatermark.from_rows(wm.to_rows())

        assert restored.summary() == wm.summary()
        assert restored.get(_key(symbol="A")) == ChunkStatus.COMPLETED
        assert restored.get(_key(symbol="B")) == ChunkStatus.NO_DATA
        assert restored.get(_key(symbol="C")) == ChunkStatus.PARTIAL
        assert restored.get(_key(symbol="D")) == ChunkStatus.FAILED

    def test_to_rows_shape(self) -> None:
        wm = FetchWatermark()
        wm.record(_key(), ChunkStatus.COMPLETED)
        rows = wm.to_rows()
        assert rows == [
            {
                "groww_symbol": "NSE-NIFTY",
                "interval": "1m",
                "start": "2021-01-01T09:15:00",
                "end": "2021-01-31T15:30:00",
                "status": "completed",
            }
        ]

    def test_from_rows_empty(self) -> None:
        assert FetchWatermark.from_rows([]).summary() == {
            "completed": 0,
            "no_data": 0,
            "partial": 0,
            "failed": 0,
        }
