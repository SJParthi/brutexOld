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
from brutex.data.option_parquet_writer import OptionSeriesCoordinate
from brutex.data.option_sink import NullOptionBarSink, OptionBarSink


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
        self.coordinates: list[OptionSeriesCoordinate] = []

    def write(
        self,
        *,
        partition_key: str,
        coordinate: OptionSeriesCoordinate,
        bars: Sequence[OptionBar],
    ) -> int:
        self.calls.append((partition_key, list(bars)))
        self.coordinates.append(coordinate)
        return len(bars)


class _FakeAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def append(self, *, action: str, subject: str, payload: dict[str, object]) -> object:
        self.rows.append({"action": action, "subject": subject, "payload": payload})
        return object()


class _FakeClosingAudit(_FakeAudit):
    """An audit double that ALSO owns a closeable handle (like ``AuditWriter``)."""

    def __init__(self) -> None:
        super().__init__()
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


def _index() -> OptionDedupIndex:
    return OptionDedupIndex(capacity=1024, fpr_target=1e-3)


def _sink(writer: _FakeWriter, audit: _FakeAudit | None = None) -> OptionBarSink:
    return OptionBarSink(index=_index(), writer=writer, audit=audit)


def _append(sink: OptionBarSink, bars: list[OptionBar]) -> int:
    return sink.append_option_bars(
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
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
    # The series coordinate is threaded to the writer as real columns
    # (DEC-EXPIRED-OPTIONS-TABLE-001): strike_offset = the REQUESTED selector.
    (coord,) = writer.coordinates
    assert coord == OptionSeriesCoordinate(
        exch_id="NSE",
        exchange_segment="NSE_FNO",
        security_id=13,
        underlying_symbol="NIFTY",
        expiry_flag="WEEK",
        expiry_code=0,
        strike_offset="ATM",
    )


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


def test_close_releases_a_closeable_audit() -> None:
    # DEC-OPTION-AUDIT-WIRING-001: close() flushes + releases the audit handle at
    # end of pull (the live AuditWriter holds an open fd) — a closeable audit is
    # closed exactly once per call, and a double-close is safe (idempotent here).
    audit = _FakeClosingAudit()
    sink = _sink(_FakeWriter(), audit)
    sink.close()
    assert audit.closes == 1
    sink.close()
    assert audit.closes == 2  # idempotent / safe to call again


def test_close_is_noop_without_an_audit() -> None:
    # audit is None → getattr(None, "close", None) is None → no-op (no error).
    _sink(_FakeWriter(), audit=None).close()


def test_close_is_noop_for_non_closeable_audit() -> None:
    # A plain audit double exposing only ``append`` (no ``close``) → no-op: the
    # OptionAuditSink protocol declares only ``append``, so the sink never assumes
    # a closeable.
    audit = _FakeAudit()
    sink = _sink(_FakeWriter(), audit)
    sink.close()  # must not raise even though _FakeAudit has no close()
    assert audit.rows == []  # close touched nothing


def _none_strike_bar(minute: int = 0) -> OptionBar:
    return OptionBar(
        timestamp=datetime(2021, 1, 4, 4, minute, tzinfo=UTC),
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        volume=250,
        open_interest=1200,
        side="CE",
        strike=None,  # side omitted the resolved strike
        spot=Decimal("17480.5"),
        iv=Decimal("0.15"),
    )


def _append_strike(sink: OptionBarSink, strike: str, bars: list[OptionBar]) -> int:
    return sink.append_option_bars(
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        expiry_flag="WEEK",
        expiry_code=0,
        strike=strike,
        drv_option_type="CALL",
        bars=bars,
    )


def test_distinct_series_never_collapse_even_with_none_strike() -> None:
    # Two DIFFERENT series (ATM vs ATM+1), same minute, same side, both with a
    # None resolved strike: the full series coordinate keeps their keys apart,
    # so neither bar is falsely deduped against the other.
    writer = _FakeWriter()
    sink = _sink(writer, _FakeAudit())
    assert _append_strike(sink, "ATM", [_none_strike_bar()]) == 1
    assert _append_strike(sink, "ATM+1", [_none_strike_bar()]) == 1  # not deduped vs ATM
    assert {pk for pk, _ in writer.calls} == {
        "NSE_FNO|13|WEEK|0|ATM|CALL",
        "NSE_FNO|13|WEEK|0|ATM+1|CALL",
    }


class TestNullOptionBarSink:
    """The options dry-run sink — counts but discards (DEC-VENDOR-PIVOT-001)."""

    def test_counts_bars_without_persisting(self) -> None:
        sink = NullOptionBarSink()
        n = sink.append_option_bars(
            security_id="13",
            exch_id="NSE",
            underlying_symbol="NIFTY",
            exchange_segment="NSE_FNO",
            expiry_flag="WEEK",
            expiry_code=1,
            strike="ATM",
            drv_option_type="CALL",
            bars=[_bar(minute=0), _bar(minute=1)],
        )
        assert n == 2

    def test_empty_bars_is_zero(self) -> None:
        assert (
            NullOptionBarSink().append_option_bars(
                security_id="13",
                exch_id="NSE",
                underlying_symbol="NIFTY",
                exchange_segment="NSE_FNO",
                expiry_flag="MONTH",
                expiry_code=1,
                strike="ATM",
                drv_option_type="PUT",
                bars=[],
            )
            == 0
        )
