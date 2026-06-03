"""Options ingest audit-chain integrity under REAL parallel workers.

DEC-DHAN-PARALLEL-INGEST-001, the riskiest fix's oracle (Golden Rule #8). The
options ingest is parallelised per underlying, so several workers reach the ONE
shared :class:`~brutex.data.option_sink.OptionBarSink` concurrently. Its
dedup→partition-write→audit tail is wrapped in a single lock so the
hash-chained :class:`~brutex.orchestrator.audit.AuditWriter` (explicitly NOT
thread-safe) advances as a strict single writer. This test runs the real
:func:`~brutex.adapters.dhan.option_universe_ingest.run_dhan_options_ingest`
over several underlyings through the REAL bounded
:class:`~brutex.orchestrator.parallel.PoolRunParallel` (genuine threads) with a
real ``AuditWriter`` to a tmp file, then asserts:

* :func:`~brutex.orchestrator.audit.verify_chain` passes (every ``prev_hash``
  links + every ``entry_hash`` recomputes) — the chain is intact, NOT corrupted
  by interleaved appends;
* the ``seq`` numbers are 0..N-1 contiguous (no gap / no duplicate);
* the number of audit rows equals the number of material (non-empty) writes.

If the single-writer lock were missing, concurrent ``append`` calls would race
``seq`` / ``prev_hash`` and ``verify_chain`` would raise — this is the oracle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from brutex.adapters.dhan.option_universe_ingest import (
    OptionUnderlying,
    run_dhan_options_ingest,
)
from brutex.data.models import OptionBar
from brutex.data.option_dedup import OptionDedupIndex
from brutex.data.option_parquet_writer import OptionSeriesCoordinate
from brutex.data.option_sink import OptionBarSink
from brutex.observability.correlation import pull_id_scope
from brutex.orchestrator.audit import AuditWriter, read_entries, verify_chain
from brutex.orchestrator.parallel import PoolRunParallel, workers_to_saturate

_START = "2021-01-01"
_END = "2021-01-08"
_TS_IN = 1609732800  # 2021-01-04 09:30 IST → in session


def _payload(strike: int) -> dict[str, object]:
    """A one-in-session-bar CE payload whose strike makes each series distinct."""
    return {
        "data": {
            "ce": {
                "timestamp": [_TS_IN],
                "open": [100],
                "high": [110],
                "low": [90],
                "close": [105],
                "volume": [250],
                "oi": [1200],
                "strike": [strike],
                "spot": [17480.5],
                "iv": [0.15],
            },
            "pe": None,
        }
    }


class _StrikeClient:
    """Returns a one-bar payload whose strike varies by the requested securityId.

    Distinct strikes per underlying keep every series' bars genuinely unique, so
    the dedup index admits them all and EVERY series produces a material write —
    maximising audit-append contention across the parallel workers.
    """

    def post_json(self, endpoint: str, body: Mapping[str, object]) -> dict[str, object]:
        del endpoint
        sid = int(str(body.get("securityId")))
        # Strike encodes the underlying so two underlyings never alias a bar.
        return _payload(strike=17000 + sid)


class _MemoryWriter:
    """An in-memory ``OptionPartitionWriter`` (no Parquet needed for the chain test)."""

    def __init__(self) -> None:
        self.writes = 0

    def write(
        self,
        *,
        partition_key: str,
        coordinate: OptionSeriesCoordinate,
        bars: Sequence[OptionBar],
    ) -> int:
        del partition_key, coordinate
        self.writes += 1
        return len(bars)


def _underlyings(n: int) -> list[OptionUnderlying]:
    return [
        OptionUnderlying(
            security_id=str(100 + i),
            exch_id="NSE",
            underlying_symbol=f"U{100 + i}",
            exchange_segment="NSE_FNO",
            instrument="OPTIDX",
            strike_offsets=1,  # ATM, ATM+1, ATM-1 → 3 strikes
            expiry_flags=("WEEK", "MONTH"),
        )
        for i in range(n)
    ]


def test_option_sink_audit_chain_intact_under_parallel(tmp_path: Path) -> None:
    audit_path = tmp_path / "options-run.jsonl"
    writer = _MemoryWriter()
    # The concrete sink: real dedup index + the single-writer audit lock under
    # test, wired to a REAL hash-chained AuditWriter.
    with AuditWriter.open(audit_path) as audit:
        sink = OptionBarSink(index=OptionDedupIndex(capacity=8192), writer=writer, audit=audit)
        underlyings = _underlyings(8)  # 8 underlyings run in parallel
        # Size the pool to saturate the budget — genuine concurrency on the sink.
        pool = PoolRunParallel(max_workers=workers_to_saturate(5.0, 3.0))
        with pull_id_scope("auditchain01"):
            report = run_dhan_options_ingest(
                _StrikeClient(),
                sink,
                underlyings=underlyings,
                start=date.fromisoformat(_START),
                end=date.fromisoformat(_END),
                run_parallel=pool,
            )
    # Every series produced a material write (distinct strikes ⇒ no dedup drop):
    # 8 underlyings × (2 flags × 3 codes × 3 strikes × 2 types) = 288 series.
    assert report.series_count == 288
    assert report.failed_series == 0

    # ── The oracle: the chain verifies despite concurrent worker appends. ──
    n_verified = verify_chain(audit_path)
    entries = read_entries(audit_path)
    assert n_verified == len(entries)
    # One audit row per material write — and it equals the writer's count.
    assert len(entries) == writer.writes
    assert len(entries) > 0
    # seq is 0..N-1 contiguous (strict single-writer advance, no gap/duplicate).
    assert [e.seq for e in entries] == list(range(len(entries)))
    # Every row is the material write action.
    assert all(e.action == "OPTION_BARS_WRITTEN" for e in entries)


def test_audit_chain_intact_with_duplicate_repull(tmp_path: Path) -> None:
    # Idempotence + chain integrity together: a second parallel pull over the
    # same range dedups every bar, so it appends ZERO new audit rows and the
    # chain still verifies.
    audit_path = tmp_path / "options-rerun.jsonl"
    writer = _MemoryWriter()
    index = OptionDedupIndex(capacity=8192)
    underlyings = _underlyings(4)
    pool = PoolRunParallel(max_workers=10)

    with AuditWriter.open(audit_path, resume=False) as audit:
        sink = OptionBarSink(index=index, writer=writer, audit=audit)
        with pull_id_scope("auditrerun01"):
            run_dhan_options_ingest(
                _StrikeClient(),
                sink,
                underlyings=underlyings,
                start=date.fromisoformat(_START),
                end=date.fromisoformat(_END),
                run_parallel=pool,
            )
    rows_after_first = len(read_entries(audit_path))
    assert rows_after_first > 0

    # Re-pull (resume the SAME chain) with the SAME dedup index → all duplicates.
    with AuditWriter.open(audit_path, resume=True) as audit:
        sink = OptionBarSink(index=index, writer=writer, audit=audit)
        with pull_id_scope("auditrerun02"):
            run_dhan_options_ingest(
                _StrikeClient(),
                sink,
                underlyings=underlyings,
                start=date.fromisoformat(_START),
                end=date.fromisoformat(_END),
                run_parallel=pool,
            )
    # No new rows (every bar deduped); the chain still verifies end-to-end.
    assert len(read_entries(audit_path)) == rows_after_first
    assert verify_chain(audit_path) == rows_after_first
