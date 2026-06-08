"""Integration — a 429 mid-cursor leaves the option partition COMPLETE or EMPTY.

DEC-OPTION-429-RECOVERY-001 (audit Gap #2), against the REAL lake-write stack:
the concrete :class:`~brutex.data.option_sink.OptionBarSink` (real
:class:`~brutex.data.option_dedup.OptionDedupIndex` + a recording partition
writer + a real hash-chained :class:`~brutex.orchestrator.audit.AuditWriter`).

A :class:`_CursorFakeClient` injects a ``429`` at a chosen cursor (window) of a
multi-page rolling-option series and we assert the partition the lake holds is
always whole or absent — never a page-holed partial — AND the audit chain stays
intact either way:

* **resumed** — a 429 that heals within the retry budget → every cursor commits,
  one material write + one audit row per window, ``verify_chain`` passes;
* **aborted** — a permanent 429 past the budget → NOTHING commits (zero writes,
  zero audit rows), and the chain still verifies.

Each cursor returns a DISTINCT in-session timestamp so the real dedup index
admits every window (no false collapse), making "all N committed" observable.
"""

# Whitebox use of the module-private `_window_dates` (the tiling oracle) is
# sanctioned for tests, mirroring `test_dhan_option_universe_ingest.py`.
# pyright: reportPrivateUsage=none

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from brutex.adapters.dhan.errors import DhanRateLimitError
from brutex.adapters.dhan.option_ingest import _window_dates, ingest_rolling_option_series
from brutex.data.models import OptionBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.option_parquet_writer import OptionSeriesCoordinate
from brutex.data.option_sink import OptionBarSink
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain

# 1609732800 = 2021-01-04 09:30 IST → in session [09:15, 15:30). Adding a few
# minutes per cursor keeps every window's bar distinct AND in session.
_TS_BASE = 1609732800

# date(2021,1,4) → date(2021,4,4) tiles into several 30-day cursors.
_START = date(2021, 1, 4)
_END = date(2021, 4, 4)


def _side(epoch: int, strike: int) -> dict[str, object]:
    """A one-in-session-bar CE side at *epoch* — unique per cursor."""
    return {
        "timestamp": [epoch],
        "open": [100],
        "high": [110],
        "low": [90],
        "close": [105],
        "volume": [250],
        "oi": [1200],
        "strike": [strike],
        "spot": [17480.5],
        "iv": [0.15],
    }


class _CursorFakeClient:
    """FakeDhanClient injecting a 429 at cursor *fail_cursor*; distinct bar per cursor.

    Each distinct ``fromDate`` is a cursor (retries reuse it → same index). Cursor
    *fail_cursor* raises :class:`DhanRateLimitError` on its first *fail_times*
    attempts (``None`` → permanently); otherwise returns one in-session CE bar
    whose timestamp + resolved strike are unique to the cursor, so the real dedup
    index admits every window.
    """

    def __init__(self, *, fail_cursor: int, fail_times: int | None) -> None:
        self._fail_cursor = fail_cursor
        self._fail_times = fail_times
        self._order: dict[str, int] = {}
        self._attempts: dict[int, int] = {}

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint
        idx = self._order.setdefault(str(body["fromDate"]), len(self._order))
        seen = self._attempts.get(idx, 0)
        self._attempts[idx] = seen + 1
        if idx == self._fail_cursor and (self._fail_times is None or seen < self._fail_times):
            raise DhanRateLimitError(f"HTTP 429 injected at cursor {idx}")
        # Unique, in-session bar per cursor (idx minutes after 09:30 IST).
        return {"data": {"ce": _side(_TS_BASE + idx * 60, 17500 + idx), "pe": None}}


class _RecordingWriter:
    """An in-memory ``OptionPartitionWriter`` recording committed bars per partition."""

    def __init__(self) -> None:
        self.by_partition: dict[str, list[OptionBar]] = {}
        self.writes = 0

    def write(
        self,
        *,
        partition_key: str,
        coordinate: OptionSeriesCoordinate,
        bars: Sequence[OptionBar],
    ) -> int:
        del coordinate
        self.writes += 1
        self.by_partition.setdefault(partition_key, []).extend(bars)
        return len(bars)

    @property
    def total_bars(self) -> int:
        """Bars committed across every partition (one series ⇒ one partition)."""
        return sum(len(b) for b in self.by_partition.values())


def _ingest(
    client: _CursorFakeClient,
    sink: OptionBarSink,
    *,
    budget: int,
) -> object:
    return ingest_rolling_option_series(
        client,
        sink,
        security_id="13",
        exch_id="NSE",
        underlying_symbol="NIFTY",
        exchange_segment="NSE_FNO",
        instrument="OPTIDX",
        expiry_flag="WEEK",
        expiry_code=1,
        strike="ATM",
        drv_option_type="CALL",
        start=_START,
        end=_END,
        max_retry_passes=budget,
        retry_backoff=lambda _i: None,  # no real sleep
    )


def test_resumed_429_commits_whole_partition_and_chain_intact(tmp_path: Path) -> None:
    n = len(_window_dates(_START, _END))
    assert n >= 2  # a genuine multi-cursor chain
    writer = _RecordingWriter()
    audit_path = tmp_path / "options-resume.jsonl"
    # 429 at cursor 1, heals after a single retry (fail_times=1 ≤ budget=2).
    client = _CursorFakeClient(fail_cursor=1, fail_times=1)
    with AuditWriter.open(audit_path) as audit:
        sink = OptionBarSink(index=OptionDedupIndex(capacity=4096), writer=writer, audit=audit)
        with pull_id_scope("optresume01"):
            report = _ingest(client, sink, budget=2)

    assert report.complete is True  # type: ignore[attr-defined]
    assert report.bars_appended == n  # type: ignore[attr-defined]
    # The WHOLE partition committed: one unique in-session bar per cursor.
    assert writer.total_bars == n
    assert writer.writes == n  # one material write per window
    # The hash-chained audit verifies and has exactly one row per material write.
    entries = read_entries(audit_path)
    assert verify_chain(audit_path) == len(entries)
    assert len(entries) == n
    assert [e.seq for e in entries] == list(range(n))


def test_permanent_429_leaves_partition_empty_and_chain_intact(tmp_path: Path) -> None:
    n = len(_window_dates(_START, _END))
    assert n >= 2
    writer = _RecordingWriter()
    audit_path = tmp_path / "options-abort.jsonl"
    # Permanent 429 at cursor 1 — never recovers within the budget → abort.
    client = _CursorFakeClient(fail_cursor=1, fail_times=None)
    with AuditWriter.open(audit_path) as audit:
        sink = OptionBarSink(index=OptionDedupIndex(capacity=4096), writer=writer, audit=audit)
        with pull_id_scope("optabort01"):
            report = _ingest(client, sink, budget=2)

    assert report.complete is False  # type: ignore[attr-defined]
    assert report.bars_appended == 0  # type: ignore[attr-defined]
    assert report.still_failed_windows == n  # type: ignore[attr-defined]
    # EMPTY partition — NOT even the already-fetched cursor 0 was committed.
    assert writer.writes == 0
    assert writer.total_bars == 0
    assert writer.by_partition == {}
    # The audit chain is intact (zero material writes ⇒ zero rows) and verifies.
    entries = read_entries(audit_path)
    assert verify_chain(audit_path) == len(entries)
    assert entries == []
