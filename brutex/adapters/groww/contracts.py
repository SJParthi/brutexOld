"""Phase C runner — enumerate contracts for every (underlying, expiry) pair.

Implements ``PhaseDriverProtocol`` for :attr:`IngestPhase.C_CONTRACTS`.

## Flow

```
PhaseCDriver.run()
  1. Enumerate one work item per (exchange, underlying, expiry_date).
     Input is the collected_expiries dict from Phase B:
         {(exchange, underlying): [expiry1, expiry2, ...]}
  2. Each item calls adapter.pull_contracts(exchange, underlying, expiry).
     Response: {"contracts": ["NSE-NIFTY-02Jan25-28500-PE", ...]}
  3. On SUCCESS, the returned groww_symbols are collected into
     self._collected_contracts keyed by (exchange, underlying, expiry).
  4. PhaseRunner drives retries + completeness verification.
```

## Dependency on Phase B

Phase C requires Phase B's output. The CLI / orchestration wires it as:

    phase_b.run()
    phase_c = PhaseCDriver(
        adapter=...,
        expiries=phase_b.collected_expiries,
        ...,
    )
    phase_c.run()

## Contract format

Per ``docs/groww-sdk/backtesting.md §4``, the contracts are bare
``groww_symbol`` strings. Each looks like::

    NSE-NIFTY-02Jan25-28500-PE
    BSE-SENSEX-25Sep25-79500-CE

The Phase D driver parses these (or looks them up in the instrument
catalogue for authoritative metadata — see instruments.md §10).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
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

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class ContractsScopeError(BruteXError):
    """Raised when Phase C's SUCCESS work items all return empty
    contracts lists — upstream shape/scope change, halt the pipeline."""


# ────────────────────────────────────────────────────────────────────────
# Result dataclass
# ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContractsResult:
    """Rich summary of a Phase C run."""

    phase_result: PhaseResult
    total_work_items: int  # (exchange, underlying, expiry) tuples enumerated
    underlyings_enumerated: int
    expiries_enumerated: int  # total count across all underlyings
    total_contracts: int  # deduplicated count of distinct groww_symbols
    per_expiry_counts: dict[tuple[str, str, str], int]
    per_underlying_counts: dict[tuple[str, str], int]
    persisted_at: datetime


# ────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────


#: Type alias: the output shape of PhaseBDriver.collected_expiries.
ExpiriesInput = Mapping[tuple[str, str], Sequence[str]]


def _content_hash(exchange: str, underlying: str, expiry_date: str) -> str:
    content = f"get_contracts:{exchange}:{underlying}:{expiry_date}"
    return hashlib.sha256(content.encode()).hexdigest()


def _make_item(*, exchange: str, underlying: str, expiry_date: str) -> WorkItem:
    return WorkItem.new(
        phase=IngestPhase.C_CONTRACTS,
        content_hash=_content_hash(exchange, underlying, expiry_date),
        params={
            "method": "get_contracts",
            "exchange": exchange,
            "underlying_symbol": underlying,
            "expiry_date": expiry_date,
        },
    )


# ────────────────────────────────────────────────────────────────────────
# PhaseCDriver
# ────────────────────────────────────────────────────────────────────────


class PhaseCDriver:
    """Phase C — enumerate contracts for every (underlying, expiry) pair.

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
    expiries
        Output of Phase B — :class:`ExpiriesInput` mapping
        ``(exchange, underlying) -> list[expiry_date]``.
    subscribers
        Pipeline event subscribers.
    """

    phase: IngestPhase = IngestPhase.C_CONTRACTS

    def __init__(
        self,
        *,
        adapter: GrowwAdapter,
        store: WorkItemStore,
        audit: AuditWriter,
        run_id: str,
        expiries: ExpiriesInput,
        subscribers: Sequence[EventSubscriber] = (),
    ) -> None:
        self._adapter = adapter
        self._store = store
        self._audit = audit
        self._run_id = run_id
        self._expiries = {k: tuple(v) for k, v in expiries.items()}
        self._subscribers = list(subscribers)

        # Populated during run()
        self._collected_contracts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._last_result: ContractsResult | None = None

    # ── PhaseDriverProtocol ────────────────────────────────────────

    def enumerate_items(self) -> Iterable[WorkItem]:
        """Yield one work item per (exchange, underlying, expiry) triple."""
        for (exchange, underlying), expiry_list in self._expiries.items():
            for expiry_date in expiry_list:
                yield _make_item(
                    exchange=exchange,
                    underlying=underlying,
                    expiry_date=expiry_date,
                )

    def run(self) -> PhaseResult:
        """Drive Phase C to completion."""
        items = list(self.enumerate_items())
        total_items = len(items)

        def invoke(work_item: WorkItem) -> tuple[WorkItem, Any]:
            params = dict(work_item.params)
            exchange = params["exchange"]
            underlying = params["underlying_symbol"]
            expiry_date = params["expiry_date"]

            new_item, response = self._adapter.pull_contracts(
                work_item=work_item,
                exchange=exchange,
                underlying_symbol=underlying,
                expiry_date=expiry_date,
            )

            if new_item.state == WorkItemState.SUCCESS and isinstance(response, dict):
                for contract in response.get("contracts", []):
                    self._collected_contracts[(exchange, underlying, expiry_date)].add(
                        str(contract)
                    )

            return new_item, response

        runner = PhaseRunner(
            phase=IngestPhase.C_CONTRACTS,
            store=self._store,
            audit=self._audit,
            invoke=invoke,
            subscribers=self._subscribers,
            run_id=self._run_id,
        )
        phase_result = runner.run(items)

        # Stats aggregation
        total_contracts = sum(len(v) for v in self._collected_contracts.values())
        per_expiry_counts = {key: len(value) for key, value in self._collected_contracts.items()}

        # Per-underlying rollup
        per_underlying_counts: dict[tuple[str, str], int] = defaultdict(int)
        for (exchange, underlying, _expiry), contracts in self._collected_contracts.items():
            per_underlying_counts[(exchange, underlying)] += len(contracts)

        # Scope check: if everything succeeded but zero contracts → halt
        if phase_result.success > 0 and total_contracts == 0:
            raise ContractsScopeError(
                f"Phase C completed with {phase_result.success} successful "
                f"work items but ZERO contracts collected. Upstream shape "
                f"change detected; halt and investigate."
            )

        result = ContractsResult(
            phase_result=phase_result,
            total_work_items=total_items,
            underlyings_enumerated=len({k[:2] for k in self._collected_contracts})
            or len(set(self._expiries.keys())),
            expiries_enumerated=total_items,
            total_contracts=total_contracts,
            per_expiry_counts=per_expiry_counts,
            per_underlying_counts=dict(per_underlying_counts),
            persisted_at=datetime.now(UTC),
        )
        self._last_result = result

        self._audit.append(
            action="CONTRACTS_SUMMARY",
            subject=f"phase:C:run:{self._run_id}",
            payload={
                "run_id": self._run_id,
                "total_work_items": total_items,
                "total_contracts": total_contracts,
                "underlyings_enumerated": result.underlyings_enumerated,
                "expiries_enumerated": total_items,
            },
        )

        return phase_result

    # ── Accessors ──────────────────────────────────────────────────

    @property
    def last_result(self) -> ContractsResult | None:
        return self._last_result

    @property
    def collected_contracts(self) -> dict[tuple[str, str, str], list[str]]:
        """Sorted contract symbols per (exchange, underlying, expiry)."""
        return {k: sorted(v) for k, v in self._collected_contracts.items()}

    def iter_all_contracts(self) -> Iterable[tuple[str, str, str, str]]:
        """Yield (exchange, underlying, expiry, groww_symbol) tuples.

        Used by Phase D to build its work items.
        """
        for (exchange, underlying, expiry), symbols in self._collected_contracts.items():
            for symbol in sorted(symbols):
                yield (exchange, underlying, expiry, symbol)


__all__ = [
    "ContractsResult",
    "ContractsScopeError",
    "ExpiriesInput",
    "PhaseCDriver",
]
