"""Tests for `brutex.data.option_sink` — dedup → write → audit.

100% line+branch via fake writer + fake audit + a real OptionDedupIndex:
write survivors, idempotent re-pull (no write/no audit), audit-present vs
audit-None branches, and the collision-safe partition key.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from brutex.data.models import OptionBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.option_sink import OptionBarSink


def _bar(*, side: str = "CE", strike: str = "17500", minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=250,
        open_interest=1200,
        side=side,
        strike=Decimal(strike),
        spot=Decimal("17480.5"),
        iv=Decimal("0.15"),
    )


class _FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[OptionBar]]] = []

    def write(self, *, partition_key: str, bars: Sequence[OptionBar]) -> int:
        self.calls.append((partition_key, list(bars)))
        return len(bars)


class _FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        self.rows.append({"action": action, "subject": subject, "payload": payload})
        return object()


def _index() -> OptionDedupIndex:
    return OptionDedupIndex(capacity=1024, fpr_target=1e-3)


def _sink(writer: _FakeWriter, audit: _FakeAudit | None = None) -> OptionBarSink:
    return OptionBarSink(index=_index(), writer=writer, audit=audit)


def _append(sink: OptionBarSink, bars: list[OptionBar]) -> int:
    return sink.append_option_bars(
        security_id="13",
        exchange_segment="NSE_FNO",
        expiry_flag="WEEK",
        expiry_code=0,
        strike="ATM",
        drv_option_type="CALL",
        bars=bars,
    )


def test_writes_survivors_and_audits() -> None:
    writer, audit = _FakeWriter(), _FakeAudit()
    sink = _sink(writer, audit)
    written = _append(sink, [_bar(minute=0), _bar(minute=1)])
    assert written == 2
    # Partition key carries the full series coordinates.
    ((partition_key, bars),) = writer.calls
    assert partition_key == "NSE_FNO|13|WEEK|0|ATM|CALL"
    assert len(bars) == 2
    # One audit row for the material write.
    (row,) = audit.rows
    assert row["action"] == "OPTION_BARS_WRITTEN"
    assert row["subject"] == "NSE_FNO|13|WEEK|0|ATM|CALL"
    assert row["payload"] == {"written": 2, "deduped": 0}


def test_idempotent_rerun_is_noop() -> None:
    writer, audit = _FakeWriter(), _FakeAudit()
    sink = _sink(writer, audit)
    bars = [_bar(minute=0), _bar(minute=1)]
    assert _append(sink, bars) == 2
    # Re-pull the same window → all duplicates → zero survivors → no write/audit.
    assert _append(sink, bars) == 0
    assert len(writer.calls) == 1  # no second write
    assert len(audit.rows) == 1  # no second audit row


def test_partial_dedup_reports_deduped_count() -> None:
    writer, audit = _FakeWriter(), _FakeAudit()
    sink = _sink(writer, audit)
    _append(sink, [_bar(minute=0)])
    # Re-pull minute 0 (dup) + minute 1 (new) → 1 written, 1 deduped.
    written = _append(sink, [_bar(minute=0), _bar(minute=1)])
    assert written == 1
    assert audit.rows[-1]["payload"] == {"written": 1, "deduped": 1}


def test_audit_optional_none_branch() -> None:
    writer = _FakeWriter()
    sink = _sink(writer, audit=None)  # no audit sink injected
    assert _append(sink, [_bar(minute=0)]) == 1
    assert len(writer.calls) == 1  # write still happens, no audit
