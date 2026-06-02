"""Concrete expired-option sink — O(1)-dedup → write → hash-chained audit.

The write-side component the rolling-option ingest streams into
(:class:`brutex.adapters.dhan.option_ingest.OptionBarSink` protocol): it
applies the **O(1) dedup guard** (:class:`~brutex.data.option_dedup.OptionDedupIndex`),
delegates the surviving bars to an injected partition writer, and appends one
**hash-chained audit row** per material write (Golden Rule #8). The dedup
index + its writer + its audit sink are all injected, so this is 100%
unit-testable without a real lake or audit file; the in-memory dedup tier can
be swapped for the LMDB tier (:class:`~brutex.data.lmdb_membership.LmdbMembership`)
with no change here.

**Dedup identity (collision-safe).** A bar's key is the **full series
coordinate** ``(exchange_segment | security_id | expiry_flag | expiry_code |
strike | drv_option_type)`` PLUS the per-bar ``(resolved_strike, side,
epoch)`` (:func:`~brutex.data.option_dedup.key_for_option_bar`). Folding the
whole series coordinate in means two **different series** (e.g. ``ATM`` vs
``ATM+1``) can never alias — even if a side omits the resolved strike and
leaves ``bar.strike`` ``None``. Within a series, a given ``(flag, code)``
resolves to one actual expiry per trading day and the full-timestamp ``epoch``
(date + minute) disambiguates the same ``(flag, code)`` across weeks — so a
re-pull of the *same* contract/minute always dedups while genuinely-new bars
never do.

Idempotent: a duplicate re-pull yields zero survivors → no write, no audit
row (CLAUDE.md §2 Rule 7). O(1) per bar (one dedup probe + one append).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from brutex.data.models import OptionBar
from brutex.data.option_dedup import OptionDedupIndex, unique_option_bars
from brutex.data.option_parquet_writer import OptionSeriesCoordinate

#: Field separator for the descriptive partition key (mirrors the dedup sep).
_KEY_SEP: str = "|"

#: Audit action stamped on every material option-bar write.
_AUDIT_ACTION: str = "OPTION_BARS_WRITTEN"


class OptionPartitionWriter(Protocol):
    """Write-side seam: persist a deduped batch of bars for one series."""

    def write(
        self,
        *,
        partition_key: str,
        coordinate: OptionSeriesCoordinate,
        bars: Sequence[OptionBar],
    ) -> int:
        """Persist *bars* under *partition_key* + *coordinate*; return count written."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class OptionAuditSink(Protocol):
    """The ``append`` slice of the hash-chained audit writer the sink uses."""

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        """Append one audit entry; return the writer's result."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


class OptionBarSink:
    """Dedup + write + audit for expired-option bars (one series per call).

    Implements the :class:`OptionBarSink` protocol the rolling-option ingest
    expects (``append_option_bars(...) -> int``).

    Parameters
    ----------
    index
        The O(1) dedup index (in-memory or LMDB-backed exact tier).
    writer
        The injected partition writer (the concrete Parquet/DuckDB
        expired-options table is a later piece).
    audit
        Optional hash-chained audit sink; when given, one row is appended per
        non-empty write (the material action).
    """

    def __init__(
        self,
        *,
        index: OptionDedupIndex,
        writer: OptionPartitionWriter,
        audit: OptionAuditSink | None = None,
    ) -> None:
        self._index = index
        self._writer = writer
        self._audit = audit

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        """Dedup *bars*, write the survivors, audit the write; return the count.

        A duplicate re-pull (all bars already seen) is a no-op: no write, no
        audit row (idempotent). Returns the number of newly-written bars.

        The requested relative ``strike`` selector (e.g. ``"ATM"`` / ``"ATM+3"``)
        is recorded as the ``strike_offset`` column (DEC-EXPIRED-OPTIONS-TABLE-001),
        distinct from each bar's RESOLVED absolute ``strike``.
        """
        # The dedup identity is the FULL series coordinate (so two distinct
        # series can never alias — even if a side omits the resolved strike,
        # leaving bar.strike None) PLUS the per-bar (resolved strike, side,
        # epoch). The partition key IS that series identity.
        partition_key = _KEY_SEP.join(
            [exchange_segment, security_id, expiry_flag, str(expiry_code), strike, drv_option_type]
        )
        survivors = unique_option_bars(
            bars, self._index, underlying=security_id, expiry=partition_key
        )
        if not survivors:
            return 0
        coordinate = OptionSeriesCoordinate(
            exch_id=exch_id,
            exchange_segment=exchange_segment,
            security_id=int(security_id),
            underlying_symbol=underlying_symbol,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            strike_offset=strike,
        )
        written = self._writer.write(
            partition_key=partition_key, coordinate=coordinate, bars=survivors
        )
        if self._audit is not None:
            self._audit.append(
                action=_AUDIT_ACTION,
                subject=partition_key,
                payload={"written": written, "deduped": len(bars) - written},
            )
        return written


class NullOptionBarSink:
    """An :class:`OptionBarSink` that counts but discards — the options dry-run sink.

    The options analog of :class:`~brutex.data.sink.NullBarSink`: it reports the
    bar count (so dry-run totals are non-zero) without any dedup / write / audit
    side effects, so the offline options pull runs with zero dependencies
    (DEC-VENDOR-PIVOT-001).
    """

    def append_option_bars(
        self,
        *,
        security_id: str,
        exch_id: str,
        underlying_symbol: str,
        exchange_segment: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        bars: Sequence[OptionBar],
    ) -> int:
        """Discard *bars* and report the count — no persistence side effects."""
        del security_id, exch_id, underlying_symbol, exchange_segment
        del expiry_flag, expiry_code, strike, drv_option_type
        return len(bars)


__all__ = [
    "NullOptionBarSink",
    "OptionAuditSink",
    "OptionBarSink",
    "OptionPartitionWriter",
]
