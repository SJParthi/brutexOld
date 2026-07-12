"""The priority pull composition — EXISTING chains only, callables injectable.

DEC-CROSSVERIFY-PRODUCER-001 §3.8 (tonight-scope: ALL spot + NIFTY/BANKNIFTY
futures, NO options — the options-excluding cross-verify trigger). Three legs
under ONE :class:`SessionRateController` (§2 Rule 6 posture; the nightly
precedent):

(a) SPOT whole universe — the nightly enumeration verbatim
    (``select_pull_instruments("index") + select_pull_instruments("stock")``)
    with **EXPLICIT ``to_date=day``** (MANDATORY — the default caps at
    yesterday, ``pull_controller.resolve_candle_range``; spy-pinned);
(b) METADATA scoped to exactly the NIFTY + BANKNIFTY instruments,
    ``years=[day.year]`` — a new serial month's FUT contract is enumerable
    same-day (``pull_contract_candles`` never re-enumerates);
(c) FUTURES — the registry's FUT rows with ``expiry_date >= day``
    (the ``contract_resolver`` idiom; a CE/PE row or an expired FUT row never
    reaches the candle puller). SENSEX futures are NOT pulled (BSE
    derivatives are out of the fetch universe — the named follow-up
    DEC-CROSSVERIFY-SENSEX-FUT-INGEST-001).

``is_pull_halt`` errors PROPAGATE to the runner (exit 2; the durable window
ledger stays resumable) — EXCEPT :class:`~brutex.data.errors.MarketHoursHalt`,
which drains gracefully into a ``halted`` report (the runner defers the day).
Honest cost: incremental via the window ledger — a day-2 run re-fetches only
unsettled windows, typically just the new session's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.data.errors import MarketHoursHalt

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from brutex.crossverify.canonical import CanonicalIdentity
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.data.instrument_store import InstrumentStore
    from brutex.data.registry_store import ContractRow, RegistryStore
    from brutex.models import Instrument

_LOG = structlog.get_logger("brutex.crossverify.pull_leg")

#: The v1 futures pull scope (coordinator ruling 1 — SENSEX futures are a
#: probe-gated follow-up; the mapper structurally supports BSE_FNO already).
FUTURES_PULL_UNDERLYINGS: Final[tuple[str, ...]] = ("NIFTY", "BANKNIFTY")

#: The metadata leg's index instruments (NSE), matching the futures scope.
_METADATA_INDEX_SYMBOLS: Final[frozenset[str]] = frozenset(FUTURES_PULL_UNDERLYINGS)


@dataclass(frozen=True, slots=True)
class PullLegReport:
    """The pull leg's outcome — feeds the export target enumeration."""

    spot_ok: bool
    metadata_ok: bool
    futures_ok: bool
    halted: bool
    halt_reason: str | None
    fut_contracts: tuple[ContractRow, ...]  # the pulled FUT rows (expiry >= day)


def _whole_universe_targets(store: InstrumentStore) -> list[Instrument]:
    """The nightly whole-universe spot enumeration, verbatim."""
    from brutex.dashboard.universe_pull import select_pull_instruments

    eligible = store.fno_eligible_underlyings()
    candidates = store.list_index_equity_instruments()
    return select_pull_instruments(candidates, kind="index", fno_eligible=eligible) + (
        select_pull_instruments(candidates, kind="stock", fno_eligible=eligible)
    )


def live_fut_rows(registry: RegistryStore, *, day: date) -> list[ContractRow]:
    """The FUT-only, unexpired (``expiry_date >= day``) registry rows, alpha order."""
    rows = [
        row
        for underlying in FUTURES_PULL_UNDERLYINGS
        for row in registry.list_contracts(underlying=underlying)
        if row.side == "FUT" and row.expiry_date >= day
    ]
    rows.sort(key=lambda row: row.contract_symbol)
    return rows


def _resolve_pullers(
    spot_puller: Callable[..., Any] | None,
    metadata_puller: Callable[..., Any] | None,
    candle_puller: Callable[..., Any] | None,
) -> tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]]:
    """Default the three pull callables to the REAL chains (lazy imports)."""
    if spot_puller is None:
        from brutex.dashboard.pull_controller import pull_all_spot

        spot_puller = pull_all_spot
    if metadata_puller is None:
        from brutex.dashboard.metadata_pull import pull_expiries_contracts

        metadata_puller = pull_expiries_contracts
    if candle_puller is None:
        from brutex.dashboard.historical_pull import pull_contract_candles

        candle_puller = pull_contract_candles
    return spot_puller, metadata_puller, candle_puller


def _halted_report(
    *, spot_ok: bool, metadata_ok: bool, futures_ok: bool, exc: MarketHoursHalt
) -> PullLegReport:
    """Fold a graceful market-hours drain into a ``halted`` report."""
    _LOG.warning(
        "crossverify.pull_halted_market_hours",
        detail=str(exc),
        action="day deferred — the durable ledger stays resumable",
    )
    return PullLegReport(
        spot_ok=spot_ok,
        metadata_ok=metadata_ok,
        futures_ok=futures_ok,
        halted=True,
        halt_reason="market_hours",
        fut_contracts=(),
    )


def run_pull_leg(
    *,
    lake_root: Path,
    day: date,
    lookback_days: int,
    store: InstrumentStore,
    registry: RegistryStore,
    session_rate: SessionRateController,
    market_closed: Callable[[], bool] | None,
    spot_puller: Callable[..., Any] | None = None,
    metadata_puller: Callable[..., Any] | None = None,
    candle_puller: Callable[..., Any] | None = None,
) -> PullLegReport:
    """Run the three priority pull legs (spot → scoped metadata → FUT candles).

    Every leg rides the SAME injected controller; per-symbol failures ride the
    spines' quarantine (those symbols gate withheld/no-data later); pull-wide
    halts propagate — only :class:`MarketHoursHalt` drains into ``halted``.
    """
    spot_pull, metadata_pull, candle_pull = _resolve_pullers(
        spot_puller, metadata_puller, candle_puller
    )
    from_date = (day - timedelta(days=lookback_days)).isoformat()
    to_date = day.isoformat()  # EXPLICIT — the default caps at yesterday
    targets = _whole_universe_targets(store)
    try:
        spot_pull(
            targets,
            lake_root=lake_root,
            dry_run=False,
            from_date=from_date,
            to_date=to_date,
            auto_retry=True,
            until_clean=True,
            floor_store=store,
            session_rate=session_rate,
            market_closed=market_closed,
        )
    except MarketHoursHalt as exc:
        return _halted_report(spot_ok=False, metadata_ok=False, futures_ok=False, exc=exc)
    index_instruments = [
        inst
        for inst in targets
        if inst.exch_id.value == "NSE" and inst.symbol_name in _METADATA_INDEX_SYMBOLS
    ]
    if len(index_instruments) < len(_METADATA_INDEX_SYMBOLS):
        _LOG.warning(
            "crossverify.metadata_indices_missing",
            found=sorted(inst.symbol_name for inst in index_instruments),
            expected=sorted(_METADATA_INDEX_SYMBOLS),
        )
    try:
        metadata_pull(
            index_instruments,
            registry=registry,
            coverage_store=store,
            years=[day.year],
            dry_run=False,
            session_rate=session_rate,
            market_closed=market_closed,
        )
    except MarketHoursHalt as exc:
        return _halted_report(spot_ok=True, metadata_ok=False, futures_ok=False, exc=exc)
    rows = live_fut_rows(registry, day=day)
    try:
        candle_pull(
            rows,
            lake_root=lake_root,
            dry_run=False,
            intervals=("1minute",),
            registry=registry,
            from_date=from_date,
            to_date=to_date,
            until_clean=True,
            session_rate=session_rate,
        )
    except MarketHoursHalt as exc:
        return _halted_report(spot_ok=True, metadata_ok=True, futures_ok=False, exc=exc)
    return PullLegReport(
        spot_ok=True,
        metadata_ok=True,
        futures_ok=True,
        halted=False,
        halt_reason=None,
        fut_contracts=tuple(rows),
    )


def run_settle_retry(
    *,
    lake_root: Path,
    day: date,
    lookback_days: int,
    affected: Sequence[CanonicalIdentity],
    store: InstrumentStore,
    registry: RegistryStore,
    session_rate: SessionRateController,
    market_closed: Callable[[], bool] | None,
    spot_puller: Callable[..., Any] | None = None,
    candle_puller: Callable[..., Any] | None = None,
) -> PullLegReport:
    """Bounded re-pull of ONLY the *affected* symbols/contracts (step G2).

    Same call shapes as :func:`run_pull_leg` with the target set narrowed to
    the affected spot symbols / FUT contract rows. No metadata leg (the chain
    was enumerated minutes ago). O(affected).
    """
    spot_pull, _metadata_pull, candle_pull = _resolve_pullers(spot_puller, None, candle_puller)
    from_date = (day - timedelta(days=lookback_days)).isoformat()
    to_date = day.isoformat()
    spot_coords = {
        (identity.exchange, identity.lake_symbol)
        for identity in affected
        if identity.lake_segment in ("INDEX", "CASH")
    }
    fut_symbols = {identity.lake_symbol for identity in affected if identity.lake_segment == "FNO"}
    spot_targets = [
        inst
        for inst in _whole_universe_targets(store)
        if (inst.exch_id.value, inst.symbol_name) in spot_coords
    ]
    fut_rows = [
        row for row in live_fut_rows(registry, day=day) if row.contract_symbol in fut_symbols
    ]
    if spot_targets:
        try:
            spot_pull(
                spot_targets,
                lake_root=lake_root,
                dry_run=False,
                from_date=from_date,
                to_date=to_date,
                auto_retry=True,
                until_clean=True,
                floor_store=store,
                session_rate=session_rate,
                market_closed=market_closed,
            )
        except MarketHoursHalt as exc:
            return _halted_report(spot_ok=False, metadata_ok=True, futures_ok=False, exc=exc)
    if fut_rows:
        try:
            candle_pull(
                fut_rows,
                lake_root=lake_root,
                dry_run=False,
                intervals=("1minute",),
                registry=registry,
                from_date=from_date,
                to_date=to_date,
                until_clean=True,
                session_rate=session_rate,
            )
        except MarketHoursHalt as exc:
            return _halted_report(spot_ok=True, metadata_ok=True, futures_ok=False, exc=exc)
    return PullLegReport(
        spot_ok=True,
        metadata_ok=True,
        futures_ok=True,
        halted=False,
        halt_reason=None,
        fut_contracts=tuple(fut_rows),
    )


__all__ = [
    "FUTURES_PULL_UNDERLYINGS",
    "PullLegReport",
    "live_fut_rows",
    "run_pull_leg",
    "run_settle_retry",
]
