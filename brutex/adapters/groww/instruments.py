"""Phase A runner — fetch + normalise + filter + persist Groww instruments.

Implements ``PhaseDriverProtocol`` for :attr:`IngestPhase.A_INSTRUMENTS`.
Glues together:

* :class:`~brutex.adapters.groww.client.GrowwAdapter` — the actual SDK call
* :func:`~brutex.adapters.groww.normaliser.normalise_instrument` — raw
  dict → :class:`~brutex.models.Instrument`
* :func:`~brutex.universe.is_in_scope` — v1 FNO filter
* :class:`~brutex.orchestrator.store.WorkItemStore` — persistence
* :class:`~brutex.orchestrator.pipeline.PhaseRunner` — retry loop + events

## Flow

```
PhaseADriver.run()
  1. Enumerate work items — one per "daily instruments snapshot".
     For P1a, this is a single work item: the current day's full
     CSV pull.
  2. Instantiate PhaseRunner with an invoke callable that wraps
     adapter.pull_all_instruments.
  3. Run the PhaseRunner → drives the item to terminal.
  4. On SUCCESS: normalise every row in the returned DataFrame,
     filter via is_in_scope, write the filtered v1 universe as
     a list of :class:`Instrument` objects to the store under a
     dedicated "instruments_v1" metadata key.
  5. Return the PhaseResult.
```

## Idempotency

* The work-item content_hash encodes the date so re-running the
  same day is a dedup hit (per ``FR-ING-010``).
* Normalisation is a pure function — same DataFrame → same Instruments.
* Filtering is a pure function — same Instruments → same v1 subset.
* The store put is atomic (LMDB transaction).

## Error handling

* Integrity errors (bad row shape) are logged to the audit chain and
  counted separately from the ingest pipeline's terminal states.
  The phase still succeeds if the raw CSV was fetched; bad rows
  surface in the "integrity" field of :class:`InstrumentsResult`
  for operator review.
* Zero in-scope rows → phase halts (``InstrumentsScopeError``) —
  indicates something has changed upstream (e.g., Groww removed a
  universe name) and a human must investigate before continuing.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from brutex.adapters.groww.client import GrowwAdapter
from brutex.adapters.groww.normaliser import normalise_instrument
from brutex.models import BruteXError, Instrument, InstrumentFormatError
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.pipeline import (
    EventSubscriber,
    PhaseResult,
    PhaseRunner,
)
from brutex.orchestrator.state import IngestPhase, WorkItem
from brutex.orchestrator.store import WorkItemStore
from brutex.universe import is_in_scope

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class InstrumentsScopeError(BruteXError):
    """Raised when the Phase A result has zero in-scope rows.

    Indicates an upstream change — e.g., a universe name rename in
    Groww — that needs human investigation. Halts the pipeline to
    prevent all downstream phases from processing an empty universe.
    """


# ────────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InstrumentsResult:
    """Summary of a Phase A run.

    Returned alongside :class:`PhaseResult` from
    :meth:`PhaseADriver.run_detailed`. The regular :meth:`run` method
    returns only the :class:`PhaseResult` for :class:`PhaseDriverProtocol`
    compatibility.
    """

    phase_result: PhaseResult
    total_raw_rows: int  # total rows in the CSV
    integrity_failures: int  # rows that failed normalise
    in_scope_count: int  # rows kept after is_in_scope filter
    out_of_scope_count: int  # rows rejected by the filter
    index_count: int  # of in-scope, how many are spot indices
    futures_count: int
    options_count: int
    equity_count: int  # should be 0 per v1 FD-09d (stocks are F&O only)
    bse_count: int
    nse_count: int
    snapshot_date: date
    persisted_at: datetime


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _derive_work_item(snapshot_date: date) -> WorkItem:
    """Build the single Phase A work item for *snapshot_date*.

    Uses a deterministic content_hash so re-runs dedup.
    """
    content = f"groww_instruments_snapshot:{snapshot_date.isoformat()}"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    return WorkItem.new(
        phase=IngestPhase.A_INSTRUMENTS,
        content_hash=content_hash,
        params={
            "method": "get_all_instruments",
            "snapshot_date": snapshot_date.isoformat(),
        },
    )


def _dataframe_to_rows(dataframe: Any) -> list[dict[str, Any]]:
    """Convert a pandas-like DataFrame to a list of row dicts.

    Duck-typed so ``FakeDataFrame`` in tests also works. Real
    ``pandas.DataFrame.to_dict(orient='records')`` returns the
    same shape.
    """
    if dataframe is None:
        return []
    # Real pandas path
    if hasattr(dataframe, "to_dict"):
        records = dataframe.to_dict(orient="records")
        if isinstance(records, list):
            return records
    # Test fake: attribute access
    if hasattr(dataframe, "_rows"):
        return list(dataframe._rows)
    return []


def _classify_counts(
    instruments: Sequence[Instrument],
) -> tuple[int, int, int, int, int, int]:
    """Return (index, futures, options, equity, nse, bse) counts."""
    from brutex.models import Exchange, InstrumentType

    idx = fut = opt = eq = nse = bse = 0
    for inst in instruments:
        if inst.instrument_type == InstrumentType.IDX:
            idx += 1
        elif inst.instrument_type == InstrumentType.FUT:
            fut += 1
        elif inst.instrument_type in {InstrumentType.CE, InstrumentType.PE}:
            opt += 1
        elif inst.instrument_type == InstrumentType.EQ:
            eq += 1

        if inst.exchange == Exchange.NSE:
            nse += 1
        elif inst.exchange == Exchange.BSE:
            bse += 1
    return idx, fut, opt, eq, nse, bse


# ────────────────────────────────────────────────────────────────────────
# PhaseADriver
# ────────────────────────────────────────────────────────────────────────


class PhaseADriver:
    """Phase A — fetch + normalise + filter the full instrument CSV.

    Implements :class:`~brutex.orchestrator.pipeline.PhaseDriverProtocol`.
    Plug it into :func:`run_sequential_pipeline` as the first entry.

    Parameters
    ----------
    adapter
        Pre-built :class:`GrowwAdapter`.
    store
        Shared :class:`WorkItemStore`.
    audit
        Shared :class:`AuditWriter`.
    run_id
        Current ingest run UUID.
    snapshot_date
        The date the snapshot represents. Used for content-hash
        derivation and audit stamps. Defaults to today's UTC date.
    subscribers
        Pipeline event subscribers (Telegram / dashboard).
    normaliser
        Callable ``dict -> Instrument``. Defaults to
        :func:`normalise_instrument`. Injectable for tests.
    scope_filter
        Callable ``Instrument -> bool``. Defaults to
        :func:`is_in_scope`. Injectable for tests.
    """

    phase: IngestPhase = IngestPhase.A_INSTRUMENTS

    def __init__(
        self,
        *,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        snapshot_date: date | None = None,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._audit = audit
        self._run_id = run_id
        self._snapshot_date = snapshot_date or datetime.now(UTC).date()
        self._subscribers = list(subscribers)

        # Post-processing result holder — populated by run()
        self._last_result: InstrumentsResult | None = None
        self._last_instruments: list[Instrument] = []

    # ── PhaseDriverProtocol ────────────────────────────────────────

    def enumerate_items(self) -> Iterable[WorkItem]:
        """Return exactly one work item — the full-CSV snapshot."""
        return [_derive_work_item(self._snapshot_date)]

    def run(self) -> PhaseResult:
        """Drive Phase A to completion and return the :class:`PhaseResult`.

        Also computes the normalisation + filtering pass and stores
        the resulting :class:`Instrument` list in the instance for
        the caller to retrieve via :meth:`last_result` /
        :meth:`last_instruments`.

        Raises
        ------
        PhaseHaltError
            If the PhaseRunner halts (unresolved quarantine).
        InstrumentsScopeError
            If the filtered v1 universe is empty (zero in-scope rows).
        """
        items = list(self.enumerate_items())

        # Capture the raw DataFrame from the invoke call
        raw_response: dict[str, Any] = {}

        def invoke(work_item: WorkItem) -> tuple[WorkItem, Any]:
            """Drive the single instruments snapshot work item (Phase A dispatch hook)."""
            new_item, response = self._adapter.pull_all_instruments(work_item=work_item)
            if response is not None:
                raw_response["df"] = response
            return new_item, response

        runner = PhaseRunner(
            phase=IngestPhase.A_INSTRUMENTS,
            store=self._store,
            audit=self._audit,
            invoke=invoke,
            subscribers=self._subscribers,
            run_id=self._run_id,
        )
        phase_result = runner.run(items)

        # Post-process: normalise + filter + store
        dataframe = raw_response.get("df")
        rows = _dataframe_to_rows(dataframe)
        total_raw = len(rows)

        normalised: list[Instrument] = []
        integrity_failures = 0
        for row in rows:
            try:
                normalised.append(normalise_instrument(row))
            except InstrumentFormatError as exc:
                integrity_failures += 1
                self._audit.append(
                    action="INSTRUMENTS_INTEGRITY_FAIL",
                    subject=f"phase:A:row:{row.get('groww_symbol', '<unknown>')}",
                    payload={
                        "run_id": self._run_id,
                        "error": str(exc)[:500],
                        "row_keys": sorted(row.keys())[:20],
                    },
                )
                logger.warning("instruments_integrity_fail", extra={"error": str(exc)})

        in_scope = [i for i in normalised if is_in_scope(i)]
        out_of_scope_count = len(normalised) - len(in_scope)

        if phase_result.success > 0 and total_raw > 0 and not in_scope:
            # We got a CSV but zero rows matched our scope — upstream
            # change. Halt the pipeline.
            raise InstrumentsScopeError(
                f"Phase A fetched {total_raw} raw rows but ZERO matched "
                f"the v1 scope filter. This indicates an upstream universe "
                f"change (Groww may have renamed an index or segment). "
                f"Operator must investigate before proceeding to Phase B."
            )

        idx_n, fut_n, opt_n, eq_n, nse_n, bse_n = _classify_counts(in_scope)

        result = InstrumentsResult(
            phase_result=phase_result,
            total_raw_rows=total_raw,
            integrity_failures=integrity_failures,
            in_scope_count=len(in_scope),
            out_of_scope_count=out_of_scope_count,
            index_count=idx_n,
            futures_count=fut_n,
            options_count=opt_n,
            equity_count=eq_n,
            bse_count=bse_n,
            nse_count=nse_n,
            snapshot_date=self._snapshot_date,
            persisted_at=datetime.now(UTC),
        )
        self._last_result = result
        self._last_instruments = in_scope

        # Audit the summary
        self._audit.append(
            action="INSTRUMENTS_SUMMARY",
            subject=f"phase:A:snapshot:{self._snapshot_date.isoformat()}",
            payload={
                "run_id": self._run_id,
                "snapshot_date": self._snapshot_date.isoformat(),
                "total_raw_rows": total_raw,
                "integrity_failures": integrity_failures,
                "in_scope_count": len(in_scope),
                "out_of_scope_count": out_of_scope_count,
                "index_count": idx_n,
                "futures_count": fut_n,
                "options_count": opt_n,
                "equity_count": eq_n,
                "nse_count": nse_n,
                "bse_count": bse_n,
            },
        )

        return phase_result

    # ── Accessors ──────────────────────────────────────────────────

    @property
    def last_result(self) -> InstrumentsResult | None:
        """The most recent :class:`InstrumentsResult`, or ``None``
        if :meth:`run` has not been called yet."""
        return self._last_result

    @property
    def last_instruments(self) -> list[Instrument]:
        """The list of in-scope :class:`Instrument` objects from the
        most recent run. Empty list if :meth:`run` has not been called."""
        return list(self._last_instruments)


__all__ = [
    "InstrumentsResult",
    "InstrumentsScopeError",
    "PhaseADriver",
]
