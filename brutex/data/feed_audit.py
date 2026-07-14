"""``FeedAuditSink`` — a hash-chained audit decorator for non-default feed writes.

DEC-FEED-INTERFACE-001 (MIGRATION.md #1792), Golden Rule #8. Every material
write to a NON-default feed's nested lake tree appends one line to a per-feed
hash-chained JSONL audit log at ``<feed_root>/.audit/ingest-chain.jsonl``, on the
COMMON orchestrator SHA-256 :class:`~brutex.orchestrator.audit.AuditWriter`
chain. Two actions:

* ``FEED_TREE_INIT`` — written exactly once, the first time a batch lands in a
  fresh feed tree: ``{feed_id, rel_root, code_commit}``.
* ``FEED_WRITE_BATCH`` — one per :meth:`append` batch: partition coordinates,
  ``bars_appended`` / ``bars_net_new``, the touched month labels, and the
  keep-first conflict count.

:class:`FeedAuditSink` is a transparent :class:`~brutex.data.sink.BarSink`
decorator: it delegates to an inner sink and returns that sink's
:class:`~brutex.data.sink.SinkAppendResult` **byte-equal** (the same object).
The audit record is best-effort + **fail-soft** — a chain-write failure
(:class:`~brutex.orchestrator.audit.AuditError` / ``OSError``) is logged once
(WARN-once latch) and never propagated, so a full ``.audit`` disk can never fail
a real pull. Inner-sink exceptions are NOT caught (a real write failure must
surface). It is **never constructed on the default (Groww) path** —
:func:`~brutex.feeds.binding.build_feed_sink` wraps only non-default feeds, so
the default write path executes byte-zero new code (the byte-identity guarantee,
DEC-FEED-LAKE-ROOT-001).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

import structlog

from brutex.data.feed_root import FEEDS_DIR_NAME
from brutex.data.models import CandleBar
from brutex.data.partition import partition_for_timestamp
from brutex.data.sink import BarSink, SinkAppendResult
from brutex.orchestrator.audit import AuditError, AuditWriter

_LOG = structlog.get_logger("brutex.data.feed_audit")

#: The ``actor`` stamped on every entry of a feed's ingest chain — distinguishes
#: feed-write records from the orchestrator's own ``"brutex"`` chain.
FEED_ACTOR: Final[str] = "feed"

#: Relative location of a feed's ingest audit chain under its feed root.
FEED_AUDIT_RELPATH: Final[tuple[str, str]] = (".audit", "ingest-chain.jsonl")

#: The two audit actions this sink emits.
ACTION_TREE_INIT: Final[str] = "FEED_TREE_INIT"
ACTION_WRITE_BATCH: Final[str] = "FEED_WRITE_BATCH"


def _month_labels(bars: Sequence[CandleBar]) -> list[str]:
    """Sorted distinct ``YYYY-MM`` labels of the partitions *bars* touch."""
    months: set[str] = set()
    for bar in bars:
        year, month = partition_for_timestamp(bar.timestamp)
        months.add(f"{year:04d}-{month:02d}")
    return sorted(months)


class FeedAuditSink:
    """A :class:`~brutex.data.sink.BarSink` decorator that hash-chains each write.

    Parameters
    ----------
    inner
        The wrapped sink (typically a :class:`~brutex.data.sink.ParquetBarSink`
        pointed at the feed's nested tree). Its result is returned unchanged.
    feed_id
        The feed this tree belongs to (already validated by the binding).
    feed_root
        The feed's effective lake root (``<lake_root>/feeds/<feed_id>``). The
        audit chain lives at ``<feed_root>/.audit/ingest-chain.jsonl``.
    code_commit
        The code commit recorded once in ``FEED_TREE_INIT`` (Golden Rule #4
        provenance). ``None`` when the caller cannot resolve it — recorded
        honestly as ``null``, never fabricated.
    """

    def __init__(
        self,
        *,
        inner: BarSink,
        feed_id: str,
        feed_root: Path,
        code_commit: str | None = None,
    ) -> None:
        self._inner = inner
        self._feed_id = feed_id
        self._feed_root = feed_root
        self._code_commit = code_commit
        self._chain_path = feed_root / FEED_AUDIT_RELPATH[0] / FEED_AUDIT_RELPATH[1]
        self._warned = False

    @property
    def inner(self) -> BarSink:
        """Return the wrapped inner sink."""
        return self._inner

    @property
    def chain_path(self) -> Path:
        """Return the path of this feed's ingest audit chain file."""
        return self._chain_path

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
        """Delegate to the inner sink, then record the batch (fail-soft).

        Returns the inner sink's :class:`~brutex.data.sink.SinkAppendResult`
        unchanged. The audit record runs AFTER the write so it captures the real
        outcome; a record failure is swallowed with one WARN.
        """
        result = self._inner.append(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
            merge_mode=merge_mode,
        )
        self._record(
            exchange=exchange,
            segment=segment,
            symbol=symbol,
            interval=interval,
            bars=bars,
            result=result,
        )
        return result

    def _record(
        self,
        *,
        exchange: str,
        segment: str,
        symbol: str,
        interval: str,
        bars: Sequence[CandleBar],
        result: SinkAppendResult,
    ) -> None:
        """Append ``FEED_TREE_INIT`` (once) + ``FEED_WRITE_BATCH`` — fail-soft."""
        subject = f"{self._feed_id}/{exchange}/{segment}/{symbol}/{interval}"
        conflicts = sum(len(wr.conflicts) for wr in result.write_results)
        try:
            first = not self._chain_path.exists()
            with AuditWriter.open(
                self._chain_path, actor=FEED_ACTOR, resume=self._chain_path.exists()
            ) as writer:
                if first:
                    writer.append(
                        action=ACTION_TREE_INIT,
                        subject=f"{self._feed_id}",
                        payload={
                            "feed_id": self._feed_id,
                            "rel_root": f"{FEEDS_DIR_NAME}/{self._feed_id}",
                            "code_commit": self._code_commit,
                        },
                    )
                writer.append(
                    action=ACTION_WRITE_BATCH,
                    subject=subject,
                    payload={
                        "exchange": exchange,
                        "segment": segment,
                        "symbol": symbol,
                        "interval": interval,
                        "bars_appended": result.bars_appended,
                        "bars_net_new": result.bars_net_new,
                        "partitions_written": result.partitions_written,
                        "months": _month_labels(bars),
                        "conflicts": conflicts,
                    },
                )
        except (AuditError, OSError) as exc:
            if not self._warned:
                self._warned = True
                _LOG.warning(
                    "feed_audit.record_failed",
                    feed_id=self._feed_id,
                    chain=repr(str(self._chain_path)),
                    error=repr(str(exc)),
                )


__all__ = [
    "ACTION_TREE_INIT",
    "ACTION_WRITE_BATCH",
    "FEED_ACTOR",
    "FEED_AUDIT_RELPATH",
    "FeedAuditSink",
]
