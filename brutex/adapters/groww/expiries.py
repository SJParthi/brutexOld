"""Phase B runner — enumerate historical expiries for every underlying.

Implements ``PhaseDriverProtocol`` for :attr:`IngestPhase.B_EXPIRIES`.

## Flow

```
PhaseBDriver.run()
  1. Enumerate work items: one per (exchange, underlying, year) tuple.
     For v1 that is 8 indices + ~215 stocks × 7 years ≈ 1,560 items.
  2. Each item calls adapter.pull_expiries(exchange, underlying, year).
     Response is {"expiries": ["2024-01-25", ...]}.
  3. On SUCCESS, the returned expiry dates are collected into
     self._collected_expiries keyed by (exchange, underlying) so
     Phase C can iterate them directly.
  4. PhaseRunner handles retry + quarantine + completeness verification.
```

## Dependency on Phase A

Phase B requires a list of ``UnderlyingSpec`` describing which
underlyings to enumerate. In the normal sequential pipeline this
comes from Phase A's output:

    phase_a.run()
    underlyings = _extract_underlyings(phase_a.last_instruments)
    phase_b = PhaseBDriver(adapter=..., underlyings=underlyings, ...)
    phase_b.run()

For tests and bespoke runs, the caller can pass any list of
``UnderlyingSpec``.

## Resume semantics

The work-item content_hash is a deterministic sha256 over
(exchange, underlying, year). Re-running the same phase on the same
store is an O(1) dedup hit per item — any SUCCESS/NO_DATA/QUARANTINED
item in the cache is skipped.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from brutex.adapters.groww.client import GrowwAdapter
from brutex.models import BruteXError
from brutex.orchestrator.audit import AuditWriter
from brutex.orchestrator.pipeline import (
    EventSubscriber,
    PhaseResult,
    PhaseRunner,
)
from brutex.orchestrator.state import IngestPhase, WorkItem, WorkItemState
from brutex.orchestrator.store import WorkItemStore
from brutex.universe import V1_INDICES, UnderlyingSpec

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class ExpiriesScopeError(BruteXError):
    """Raised when Phase B returns zero expiries across the entire
    universe — upstream change detected, halt the pipeline."""


# ────────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ExpiriesResult:
    """Rich summary of a Phase B run, exposed via
    :attr:`PhaseBDriver.last_result`."""

    phase_result: PhaseResult
    underlyings_enumerated: int
    years_enumerated: tuple[int, ...]
    total_work_items: int
    underlyings_with_expiries: int
    total_expiries: int
    per_underlying_counts: dict[tuple[str, str], int]
    persisted_at: datetime


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


def _content_hash(exchange: str, underlying: str, year: int) -> str:
    """Deterministic hash for Phase B work items."""
    content = f"get_expiries:{exchange}:{underlying}:{year}"
    return hashlib.sha256(content.encode()).hexdigest()


def _make_item(*, exchange: str, underlying: str, year: int) -> WorkItem:
    """Build a Phase B :class:`WorkItem` from one (underlying, year) pair."""
    return WorkItem.new(
        phase=IngestPhase.B_EXPIRIES,
        content_hash=_content_hash(exchange, underlying, year),
        params={
            "method": "get_expiries",
            "exchange": exchange,
            "underlying_symbol": underlying,
            "year": str(year),
        },
    )


def _current_year_utc() -> int:
    """Return the current UTC year as an integer."""
    return datetime.now(UTC).year


# ────────────────────────────────────────────────────────────────────────
# PhaseBDriver
# ────────────────────────────────────────────────────────────────────────


class PhaseBDriver:
    """Phase B — enumerate historical expiries for every F&O underlying.

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
    underlyings
        Sequence of :class:`UnderlyingSpec` to enumerate. Default is
        :data:`V1_INDICES` (the 8 named indices). Callers that want
        stock F&O too construct their own list from Phase A's output.
    start_year
        First year to enumerate (inclusive). Default 2020 per FD-08.
    end_year
        Last year to enumerate (inclusive). Default current UTC year.
    subscribers
        Pipeline event subscribers.
    """

    phase: IngestPhase = IngestPhase.B_EXPIRIES

    def __init__(
        self,
        *,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        underlyings: Sequence[UnderlyingSpec] = V1_INDICES,
        start_year: int = 2020,
        end_year: int | None = None,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._audit = audit
        self._run_id = run_id
        self._underlyings = list(underlyings)
        self._start_year = start_year
        self._end_year = end_year if end_year is not None else _current_year_utc()
        self._subscribers = list(subscribers)

        if self._end_year < self._start_year:
            raise ValueError(
                f"end_year ({self._end_year}) must be >= start_year ({self._start_year})"
            )

        # Populated during run()
        self._collected_expiries: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._last_result: ExpiriesResult | None = None

    @property
    def years(self) -> tuple[int, ...]:
        """Return the inclusive year range this driver enumerates over."""
        return tuple(range(self._start_year, self._end_year + 1))

    # ── PhaseDriverProtocol ────────────────────────────────────────

    def enumerate_items(self) -> Iterable[WorkItem]:
        """Yield one work item per (underlying, year) tuple."""
        for spec in self._underlyings:
            # Respect the underlying's live-since year — no point
            # calling get_expiries(NIFTYNXT50, year=2020) when it
            # only went live in 2024.
            first = max(self._start_year, spec.fno_live_since_year)
            for year in range(first, self._end_year + 1):
                yield _make_item(
                    exchange=spec.exchange.value,
                    underlying=spec.underlying_symbol,
                    year=year,
                )

    def run(self) -> PhaseResult:
        """Drive Phase B to completion."""
        items = list(self.enumerate_items())
        total_items = len(items)

        def invoke(work_item: WorkItem) -> tuple[WorkItem, Any]:
            """Drive a single expiries work item (Phase B dispatch hook)."""
            params = dict(work_item.params)
            exchange = params["exchange"]
            underlying = params["underlying_symbol"]
            year = int(params["year"])

            new_item, response = self._adapter.pull_expiries(
                work_item=work_item,
                exchange=exchange,
                underlying_symbol=underlying,
                year=year,
            )

            if new_item.state == WorkItemState.SUCCESS and isinstance(response, dict):
                for expiry_date in response.get("expiries", []):
                    self._collected_expiries[(exchange, underlying)].add(str(expiry_date))

            return new_item, response

        runner = PhaseRunner(
            phase=IngestPhase.B_EXPIRIES,
            store=self._store,
            audit=self._audit,
            invoke=invoke,
            subscribers=self._subscribers,
            run_id=self._run_id,
        )
        phase_result = runner.run(items)

        # Scope check — every phase-A-driven scope error pattern applies.
        total_expiries = sum(len(v) for v in self._collected_expiries.values())
        underlyings_with_expiries = sum(1 for v in self._collected_expiries.values() if v)

        if phase_result.success > 0 and total_expiries == 0:
            raise ExpiriesScopeError(
                f"Phase B completed with {phase_result.success} successful "
                f"work items but ZERO expiries collected. This indicates an "
                f"upstream change in get_expiries' response shape. Halt the "
                f"pipeline and investigate."
            )

        per_underlying_counts = {key: len(value) for key, value in self._collected_expiries.items()}

        result = ExpiriesResult(
            phase_result=phase_result,
            underlyings_enumerated=len(self._underlyings),
            years_enumerated=self.years,
            total_work_items=total_items,
            underlyings_with_expiries=underlyings_with_expiries,
            total_expiries=total_expiries,
            per_underlying_counts=per_underlying_counts,
            persisted_at=datetime.now(UTC),
        )
        self._last_result = result

        self._audit.append(
            action="EXPIRIES_SUMMARY",
            subject=f"phase:B:run:{self._run_id}",
            payload={
                "run_id": self._run_id,
                "underlyings_enumerated": len(self._underlyings),
                "total_work_items": total_items,
                "years": list(self.years),
                "underlyings_with_expiries": underlyings_with_expiries,
                "total_expiries": total_expiries,
            },
        )

        return phase_result

    # ── Accessors ──────────────────────────────────────────────────

    @property
    def last_result(self) -> ExpiriesResult | None:
        """Return the most recent :class:`ExpiriesResult`, or None before run()."""
        return self._last_result

    @property
    def collected_expiries(self) -> dict[tuple[str, str], list[str]]:
        """Sorted expiry dates per (exchange, underlying).

        Consumed by :class:`PhaseCDriver` to enumerate its work items.
        """
        return {k: sorted(v) for k, v in self._collected_expiries.items()}


__all__ = [
    "ExpiriesResult",
    "ExpiriesScopeError",
    "PhaseBDriver",
]
