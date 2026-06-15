"""Populate the F&O contracts REGISTRY for the LIVE subscription — expiries → contracts.

DEC-LIVE-LEG-RESOLUTION-001 (ledger #793). The live N-connection capture
(:func:`brutex.live.multiconn.plan_connections`) shards the subscription universe
into ``ceil(N / 1000)`` websocket connections, but today it only ever sees the
~779 SPOT instruments, so it always builds ONE connection (779 < the 1000/conn
cap) and the operator never sees the N-connection scaling engage. The reason is
upstream: :func:`brutex.dashboard.live_manifest.build_leg_rows` reads the F&O
``contracts`` registry (:class:`~brutex.data.registry_store.RegistryStore`), and on
the live setup that registry is **never populated**, so it degrades to ``legs=[]``
(``live_manifest.py:205``). This module is the missing "pull contracts → populate
the registry" step: once the CE/PE/FUT leg SYMBOLS land in the registry, the
manifest carries the legs, the universe crosses 1000, and the sharding engages
automatically.

It is the LIVE-path twin of the dashboard's
:func:`brutex.dashboard.metadata_pull.pull_expiries_contracts` (CLAUDE.md §11
COMMON): it reuses the SAME resolution primitives
(:func:`~brutex.adapters.groww.contracts.resolve_expiries` /
:func:`~brutex.adapters.groww.contracts.resolve_contracts`) and the SAME registry
upsert — never re-implementing either. The difference is intent: the dashboard
metadata pull walks the FULL ``2020 → today`` year grid for the historical lake;
this pull resolves only the **nearest N expiries** an operator wants the LIVE feed
subscribed to (the near-term active chain), bounded + operator-tunable, because the
live feed cares about the contracts trading NOW, not the whole archive. It lives in
the adapter layer (NOT ``brutex/live/``) so it can import
:class:`~brutex.data.registry_store.RegistryStore` while keeping ``brutex/live/``
itself imported-by-nothing (DEC-LIVE-DECOUPLE-001) — the live CLI calls this
adapter function exactly as it already calls the other Groww adapter primitives.

**Scope (CLAUDE.md §3):** the resolved CE/PE/FUT legs are **LAKE / live-subscription
REFERENCE_ONLY** (mirrors DEC-P0-063). They are NOT added to the brute-force engine,
the 52-condition vocabulary, or the trading universe — the brute-force engine stays
NIFTY/BANKNIFTY/SENSEX. This is a live-subscription / lake expansion ONLY.

**Pacing (CLAUDE.md §2 Rule 6):** every ``get_expiries`` / ``get_contracts`` call
rides the injected :class:`~brutex.adapters.groww.client.GrowwClient`'s ``pace``
hook — the SAME adaptive token bucket + 500/min governor the spot/option pulls use
(the live CLI wires the operator-DIRECTED 8 r/s ceiling). This module does NOT
reinvent pacing and never hardcodes a rate.

**Idempotence + crash-safety (Golden Rule #7):** the registry upserts are
``INSERT OR REPLACE`` on the contract PK, so re-running yields the SAME rows
(rerun-safe) and a partial pull (one underlying done, the process killed) leaves a
VALID registry of exactly the contracts already upserted — never an all-or-nothing
transaction that loses prior work. A per-underlying resolution failure is
quarantined (recorded, the run continues) so one bad underlying never aborts the
whole pull.

**Honest envelope (Golden Rule #10):** network-bound — one ``get_expiries`` per
underlying (per probed year) + one ``get_contracts`` per selected expiry. NOT
O(1)-total; the per-call rate gate is the injected ``client.pace``. The ACTUAL
network call surface is the operator's-Mac-only ``# pragma`` boundary in the live
CLI; the pull LOOP + the registry upsert + the nearest-N selection + the
idempotence here are fully covered with a fake SDK.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import structlog

from brutex.adapters.groww.client import GrowwClient
from brutex.adapters.groww.contracts import resolve_contracts, resolve_expiries
from brutex.adapters.groww.errors import GrowwError
from brutex.data.registry_store import RegistryStore

#: Structured logger — per-underlying INFO rollup + a WARNING on a quarantined
#: resolution failure (every event greppable in the live CLI's JSONL log).
_LOG = structlog.get_logger("brutex.adapters.groww.contract_pull")

#: Default number of nearest expiries to subscribe per underlying. Bounded +
#: operator-tunable (the CLI exposes ``--expiries``); the live feed only needs the
#: near-term active chain, not the full historical archive. Kept small so a full
#: ~750-underlying pull stays inside a reasonable call budget under the 500/min
#: governor.
DEFAULT_EXPIRIES_PER_UNDERLYING: Final[int] = 2

#: The run-id stamped on every persisted row when the caller injects none — a
#: stable, honest marker (the live CLI injects a per-pull id).
DEFAULT_PULL_RUN_ID: Final[str] = "live-contract-pull"


@dataclass(frozen=True, slots=True)
class PullTarget:
    """One ``(exchange, underlying_symbol)`` whose F&O chain to resolve.

    The ``underlying_symbol`` is the vendor F&O symbol (e.g. ``"NIFTY"``,
    ``"RELIANCE"``) — the SAME string ``get_expiries`` / ``get_contracts`` key on,
    and the SAME string the vendor stamps as the contract ``underlying_symbol``.
    """

    exchange: str
    underlying_symbol: str


@dataclass(frozen=True, slots=True)
class UnderlyingPullResult:
    """The per-underlying outcome of one :func:`pull_contracts_to_registry` step."""

    target: PullTarget
    expiries_selected: int
    contracts_persisted: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True iff resolution completed without a quarantined Groww error."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class ContractPullReport:
    """The honest aggregate outcome of one contracts-pull pass.

    Attributes
    ----------
    results
        One :class:`UnderlyingPullResult` per target, in pull order.
    """

    results: tuple[UnderlyingPullResult, ...]

    @property
    def underlyings(self) -> int:
        """How many underlyings were attempted."""
        return len(self.results)

    @property
    def expiries_selected(self) -> int:
        """Total nearest expiries selected across all underlyings."""
        return sum(r.expiries_selected for r in self.results)

    @property
    def contracts_persisted(self) -> int:
        """Total CE/PE/FUT legs upserted into the registry."""
        return sum(r.contracts_persisted for r in self.results)

    @property
    def failures(self) -> int:
        """How many underlyings were quarantined on a resolution error."""
        return sum(1 for r in self.results if not r.ok)

    def to_dict(self) -> dict[str, object]:
        """JSON-safe view for the live CLI's structured-log rollup."""
        return {
            "underlyings": self.underlyings,
            "expiries_selected": self.expiries_selected,
            "contracts_persisted": self.contracts_persisted,
            "failures": self.failures,
        }


def select_nearest_expiries(expiries: Sequence[date], *, today: date, limit: int) -> list[date]:
    """Return the *limit* expiries the LIVE feed should subscribe — nearest first.

    The live feed cares about the contracts trading NOW, so it prefers the
    UPCOMING (not-yet-expired) expiries closest to *today*. If fewer than *limit*
    upcoming expiries exist (e.g. late in a chain's life, or the vendor only
    returned past dates), the MOST-RECENT already-expired expiries backfill the
    remainder — so a still-listed but already-past near expiry is never silently
    dropped (Golden Rule #1: degrade honestly, never invent a future expiry).

    Pure + deterministic. ``limit <= 0`` returns ``[]`` (a degenerate but valid
    "subscribe no legs" request); a *limit* larger than the input returns every
    expiry. The result is sorted ASCENDING (the registry/manifest order is stable).

    Parameters
    ----------
    expiries
        The resolved expiry dates (any order; deduped by the caller's resolver).
    today
        The reference date for "upcoming vs expired".
    limit
        The maximum number of expiries to keep.

    Returns
    -------
    list[date]
        At most *limit* expiries, ascending, preferring upcoming-nearest.
    """
    if limit <= 0:
        return []
    upcoming = sorted(e for e in expiries if e >= today)
    expired = sorted((e for e in expiries if e < today), reverse=True)
    # Upcoming-nearest first; backfill with the most-recent expired if short.
    chosen = upcoming[:limit]
    if len(chosen) < limit:
        chosen.extend(expired[: limit - len(chosen)])
    return sorted(chosen)


def _resolve_underlying_expiries(
    client: GrowwClient, target: PullTarget, *, today: date
) -> list[date]:
    """Resolve a target's expiry dates across the current + next year (deduped).

    Two ``get_expiries`` calls — the current year AND next year — so a pull run in
    (say) December still discovers the January expiries of the upcoming chain that
    a current-year-only query would miss. The two date sets are unioned + deduped
    via a ``set`` (O(1) membership). The nearest-N selection is applied by the
    caller (:func:`pull_contracts_to_registry`).
    """
    seen: set[date] = set()
    for year in (today.year, today.year + 1):
        for expiry in resolve_expiries(
            client,
            exchange=target.exchange,
            underlying_symbol=target.underlying_symbol,
            year=year,
        ):
            seen.add(expiry)
    return sorted(seen)


def pull_contracts_to_registry(
    client: GrowwClient,
    registry: RegistryStore,
    *,
    targets: Sequence[PullTarget],
    expiries_per_underlying: int = DEFAULT_EXPIRIES_PER_UNDERLYING,
    now: datetime | None = None,
    today: date | None = None,
    run_id: str = DEFAULT_PULL_RUN_ID,
) -> ContractPullReport:
    """Resolve each target's nearest-N expiries → contracts → upsert into *registry*.

    The LIVE-path contracts pull. For every *target*:

    1. resolve its expiry dates (current + next year) via
       :func:`~brutex.adapters.groww.contracts.resolve_expiries`;
    2. keep the nearest *expiries_per_underlying* (:func:`select_nearest_expiries`);
    3. for each kept expiry, resolve its CE/PE/FUT contracts via
       :func:`~brutex.adapters.groww.contracts.resolve_contracts` and persist the
       expiry + each contract leg into *registry* (idempotent on the PKs).

    Every Groww call rides the injected ``client.pace`` rate gate (the live CLI
    wires the shared 8 r/s + 500/min controller). A TERMINAL Groww error on ONE
    underlying is QUARANTINED — recorded on its :class:`UnderlyingPullResult` and
    the run continues to the next underlying — so one bad / delisted symbol never
    aborts the whole pull (a non-Groww exception, a genuine bug, is NOT swallowed).

    Idempotence + crash-safety (Golden Rule #7): the upserts are per-contract
    ``INSERT OR REPLACE`` on the contract PK, so re-running yields the SAME rows and
    a process killed mid-pull leaves a VALID registry of the contracts already
    written (never an all-or-nothing rollback that loses prior underlyings).

    Parameters
    ----------
    client
        The injected :class:`~brutex.adapters.groww.client.GrowwClient` (the only
        network surface; tests pass a fake SDK behind a real client).
    registry
        The F&O contracts cache to populate (the legs the lean master discards).
    targets
        The ``(exchange, underlying_symbol)`` pairs to resolve — the F&O-eligible
        single stocks + the indices (the live CLI resolves them from the master).
    expiries_per_underlying
        How many nearest expiries to subscribe per underlying (operator-tunable;
        ``<= 0`` selects none — a valid degenerate "no legs" request).
    now
        Injected clock for the persisted ``fetched_at`` (defaults to
        ``datetime.now(UTC)``); MUST be timezone-aware (the registry validates it).
    today
        Injected date for the upcoming-vs-expired split (defaults to *now*'s UTC
        date).
    run_id
        The reproducibility id stamped on every persisted row.

    Returns
    -------
    ContractPullReport
        Per-underlying expiry/contract counts + the quarantined failures.
    """
    now = now or datetime.now(UTC)
    today = today or now.astimezone(UTC).date()
    results: list[UnderlyingPullResult] = []
    for target in targets:
        results.append(
            _pull_one_underlying(
                client,
                registry,
                target=target,
                expiries_per_underlying=expiries_per_underlying,
                now=now,
                today=today,
                run_id=run_id,
            )
        )
    report = ContractPullReport(results=tuple(results))
    _LOG.info("contract_pull.complete", **report.to_dict(), run_id=run_id)
    return report


def _pull_one_underlying(
    client: GrowwClient,
    registry: RegistryStore,
    *,
    target: PullTarget,
    expiries_per_underlying: int,
    now: datetime,
    today: date,
    run_id: str,
) -> UnderlyingPullResult:
    """Resolve + persist ONE underlying's nearest-N chain (quarantines a Groww error).

    Split out so the per-underlying ``try`` is a single, narrow block: a TERMINAL
    Groww error during resolution is caught + recorded (the run continues); the
    persistence that already happened for earlier underlyings (and earlier expiries
    of THIS underlying) survives (crash-safe — Golden Rule #7).
    """
    try:
        all_expiries = _resolve_underlying_expiries(client, target, today=today)
        selected = select_nearest_expiries(all_expiries, today=today, limit=expiries_per_underlying)
        persisted = 0
        for expiry in selected:
            registry.upsert_expiry(
                target.exchange,
                target.underlying_symbol,
                expiry,
                run_id=run_id,
                fetched_at=now,
            )
            contracts = resolve_contracts(
                client,
                exchange=target.exchange,
                underlying_symbol=target.underlying_symbol,
                expiry=expiry,
            )
            for contract in contracts:
                registry.upsert_contract(
                    target.exchange,
                    target.underlying_symbol,
                    expiry,
                    contract.symbol,
                    side=contract.contract_type,
                    strike=contract.strike,
                    run_id=run_id,
                    fetched_at=now,
                )
            persisted += len(contracts)
    except GrowwError as exc:
        _LOG.warning(
            "contract_pull.quarantine",
            exchange=target.exchange,
            underlying=target.underlying_symbol,
            error=str(exc),
        )
        return UnderlyingPullResult(
            target=target, expiries_selected=0, contracts_persisted=0, error=str(exc)
        )
    _LOG.info(
        "contract_pull.underlying",
        exchange=target.exchange,
        underlying=target.underlying_symbol,
        expiries=len(selected),
        contracts=persisted,
    )
    return UnderlyingPullResult(
        target=target, expiries_selected=len(selected), contracts_persisted=persisted
    )


__all__ = [
    "DEFAULT_EXPIRIES_PER_UNDERLYING",
    "DEFAULT_PULL_RUN_ID",
    "ContractPullReport",
    "PullTarget",
    "UnderlyingPullResult",
    "pull_contracts_to_registry",
    "select_nearest_expiries",
]
