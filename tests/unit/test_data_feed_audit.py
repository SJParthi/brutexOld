"""Unit tests for :mod:`brutex.data.feed_audit` (DEC-FEED-INTERFACE-001, #1779).

100% line + branch. Covers the transparent passthrough (byte-equal result), the
``FEED_TREE_INIT``-once + ``FEED_WRITE_BATCH``-per-append hash chain (Golden Rule
#8), the multi-month label fold, the conflict count, chain verifiability + a
tamper-fails-verify teeth check, and the fail-soft WARN-once latch.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from brutex.data.feed_audit import (
    ACTION_TREE_INIT,
    ACTION_WRITE_BATCH,
    FEED_ACTOR,
    FeedAuditSink,
)
from brutex.data.models import CandleBar
from brutex.data.parquet_writer import WriteResult
from brutex.data.sink import SinkAppendResult
from brutex.orchestrator.audit import (
    AuditChainCorruption,
    read_entries,
    verify_chain,
)


def _bar(year: int, month: int, minute: int) -> CandleBar:
    return CandleBar(
        timestamp=datetime(year, month, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100.00"),
        high=Decimal("100.00"),
        low=Decimal("100.00"),
        close=Decimal("100.00"),
        volume=1,
        open_interest=None,
    )


def _wr(conflicts: tuple[object, ...]) -> WriteResult:
    return WriteResult(
        path=Path("x.parquet"),
        row_count=1,
        raw_hash="0" * 64,
        first_ts=datetime(2024, 1, 1, tzinfo=UTC),
        last_ts=datetime(2024, 1, 1, tzinfo=UTC),
        bytes_written=1,
        bars_added=1,
        conflicts=conflicts,  # type: ignore[arg-type]
    )


class _FakeSink:
    """A minimal :class:`~brutex.data.sink.BarSink` returning queued results.

    Returns the next queued :class:`SinkAppendResult` per call; once the queue is
    exhausted it keeps returning the last one (so a single-result fixture works).
    """

    def __init__(self, *results: SinkAppendResult) -> None:
        self._results = list(results)
        self.calls: list[tuple[str, ...]] = []

    def append(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        merge_mode: str = "keep-first",
    ) -> SinkAppendResult:
        self.calls.append((exchange, segment, symbol, interval, merge_mode))
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]


def _result(*, write_results: tuple[WriteResult, ...] = ()) -> SinkAppendResult:
    return SinkAppendResult(
        bars_appended=1,
        partitions_written=len(write_results),
        write_results=write_results,
        bars_net_new=1,
    )


class TestPassthrough:
    def test_returns_inner_result_object(self, tmp_path: Path) -> None:
        res = _result()
        inner = _FakeSink(res)
        sink = FeedAuditSink(inner=inner, feed_id="feedx", feed_root=tmp_path)
        out = sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 15)],
        )
        assert out is res  # byte-equal — the SAME object
        assert inner.calls == [("NSE", "INDEX", "NSE-NIFTY", "1minute", "keep-first")]

    def test_properties(self, tmp_path: Path) -> None:
        inner = _FakeSink(_result())
        sink = FeedAuditSink(inner=inner, feed_id="feedx", feed_root=tmp_path)
        assert sink.inner is inner
        assert sink.chain_path == tmp_path / ".audit" / "ingest-chain.jsonl"


class TestChain:
    def test_init_once_then_batch_per_append(self, tmp_path: Path) -> None:
        sink = FeedAuditSink(
            inner=_FakeSink(
                _result(write_results=(_wr(("c1", "c2")),)),
                _result(),
            ),
            feed_id="feedx",
            feed_root=tmp_path,
            code_commit="deadbeef",
        )
        # first append: TREE_INIT + WRITE_BATCH; bars span two months.
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 15), _bar(2024, 2, 3)],
        )
        # second append: only WRITE_BATCH (init latched by chain existence).
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 3, 1)],
        )

        entries = read_entries(sink.chain_path)
        assert [e.action for e in entries] == [
            ACTION_TREE_INIT,
            ACTION_WRITE_BATCH,
            ACTION_WRITE_BATCH,
        ]
        assert all(e.actor == FEED_ACTOR for e in entries)

        init = entries[0]
        assert init.payload == {
            "feed_id": "feedx",
            "rel_root": "feeds/feedx",
            "code_commit": "deadbeef",
        }
        batch1 = entries[1]
        assert batch1.payload["months"] == ["2024-01", "2024-02"]
        assert batch1.payload["conflicts"] == 2
        assert batch1.payload["bars_appended"] == 1
        assert batch1.payload["bars_net_new"] == 1
        assert entries[2].payload["months"] == ["2024-03"]
        assert entries[2].payload["conflicts"] == 0

        # Golden Rule #8: the chain verifies end-to-end.
        assert verify_chain(sink.chain_path) == 3

    def test_default_code_commit_is_null(self, tmp_path: Path) -> None:
        sink = FeedAuditSink(inner=_FakeSink(_result()), feed_id="feedx", feed_root=tmp_path)
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 1)],
        )
        assert read_entries(sink.chain_path)[0].payload["code_commit"] is None

    def test_tamper_fails_verify(self, tmp_path: Path) -> None:
        sink = FeedAuditSink(inner=_FakeSink(_result()), feed_id="feedx", feed_root=tmp_path)
        sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 1)],
        )
        lines = sink.chain_path.read_text(encoding="utf-8").splitlines()
        # Corrupt the first entry's payload without recomputing the hash chain.
        lines[0] = lines[0].replace('"feedx"', '"tampered"', 1)
        sink.chain_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            verify_chain(sink.chain_path)
        except AuditChainCorruption:
            return
        raise AssertionError("tampered chain should have failed verify")


class TestFailSoft:
    def test_record_failure_is_swallowed_and_warns_once(self, tmp_path: Path) -> None:
        # Make the `.audit` path a FILE so AuditWriter.open's parent mkdir raises
        # OSError — the record must fail soft and NOT break the (successful) write.
        (tmp_path / ".audit").write_text("blocking file", encoding="utf-8")
        res = _result()
        sink = FeedAuditSink(inner=_FakeSink(res), feed_id="feedx", feed_root=tmp_path)

        out1 = sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 1, 1)],
        )
        assert out1 is res  # write still succeeded
        assert sink._warned is True  # pyright: ignore[reportPrivateUsage]

        # Second failing append: already-latched, no second warn path.
        out2 = sink.append(
            exchange="NSE",
            segment="INDEX",
            symbol="NSE-NIFTY",
            interval="1minute",
            bars=[_bar(2024, 2, 1)],
        )
        assert out2 is res
        assert sink._warned is True  # pyright: ignore[reportPrivateUsage]
