"""The priority pull composition — SPOT ONLY, EXISTING chains, injectable.

DEC-CROSSVERIFY-PRODUCER-001 §3.8 as amended by DEC-CROSSVERIFY-SPOT-ONLY-001
(MIGRATION.md #1788 — the operator contract amendment): the export contract
is SPOT-ONLY, so the producer pulls exactly ONE leg — the WHOLE spot
universe (every admitted index + every admitted stock, F&O-eligible AND
reference-only NTM names alike) under ONE
:class:`SessionRateController` (§2 Rule 6 posture; the nightly precedent).
The #1776 metadata + futures-candle legs are REMOVED (no registry read, no
``pull_expiries_contracts``, no ``pull_contract_candles`` — zero F&O vendor
calls), and with them the round-1 date-type dual-shape law collapses to the
spot chain's alone: ``pull_all_spot`` takes ISO **strings** (``str | None``)
with **EXPLICIT ``to_date=day``** (MANDATORY — the default caps at
yesterday, ``pull_controller.resolve_candle_range``; spy-pinned).

Whole-universe law (#1788 census): the spot target set is the COMMON
classifier (:func:`brutex.dashboard.universe_query.classify_instrument`)
over the store's IDX+EQ candidates WITH ``reference_only_security_ids``
threaded — indices first then stocks, each alpha-sorted (the
``select_pull_instruments`` order). Threading the reference ids widens the
per-kind classification to the admitted NTM constituents by SECURITY_ID (the
``/universe`` full-membership precedent, DEC-INDEX-CONSTITUENTS-FULL-001);
on a production LEAN store the ticker arm already admits every persisted EQ
(``fno_eligible_underlyings``'s persisted-EQ union — eligible by
construction), so this is a widening belt, never a narrowing.

``is_pull_halt`` errors PROPAGATE to the runner (exit 2; the durable window
ledger stays resumable) — EXCEPT :class:`~brutex.data.errors.MarketHoursHalt`,
which drains gracefully into a ``halted`` report (the runner defers the day).
Honest cost: incremental via the window ledger — a day-2 run re-fetches only
unsettled windows, typically just the new session's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import structlog

from brutex.data.errors import MarketHoursHalt

if TYPE_CHECKING:  # pragma: no cover — typing-only imports
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from brutex.crossverify.canonical import CanonicalIdentity
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.data.instrument_store import InstrumentStore
    from brutex.models import Instrument

_LOG = structlog.get_logger("brutex.crossverify.pull_leg")


@dataclass(frozen=True, slots=True)
class PullLegReport:
    """The pull leg's outcome — spot-only since DEC-CROSSVERIFY-SPOT-ONLY-001."""

    spot_ok: bool
    halted: bool
    halt_reason: str | None


def whole_universe_spot_targets(store: InstrumentStore) -> list[Instrument]:
    """The WHOLE spot-universe enumeration — indices then stocks, each alpha.

    The ONE COMMON classifier (`classify_instrument`) over the store's
    IDX+EQ candidates with the reference-only SECURITY_ID set threaded, so
    the admitted NTM constituents classify ``"stock"`` too (#1788 —
    the export must cover the ENTIRE spot universe, ~25 indices + ~751
    stocks, never merely the F&O-eligible subset).
    """
    from brutex.dashboard.universe_query import classify_instrument

    eligible = store.fno_eligible_underlyings()
    reference_ids = store.reference_only_security_ids()
    candidates = store.list_index_equity_instruments()
    indices: list[Instrument] = []
    stocks: list[Instrument] = []
    for instrument in candidates:
        kind = classify_instrument(instrument, eligible, reference_ids)
        if kind == "index":
            indices.append(instrument)
        elif kind == "stock":
            stocks.append(instrument)
    indices.sort(key=lambda instrument: instrument.symbol_name)
    stocks.sort(key=lambda instrument: instrument.symbol_name)
    return indices + stocks


def _resolve_spot_puller(spot_puller: Callable[..., Any] | None) -> Callable[..., Any]:
    """Default the spot pull callable to the REAL chain (lazy import)."""
    if spot_puller is None:
        from brutex.dashboard.pull_controller import pull_all_spot

        spot_puller = pull_all_spot
    return spot_puller


def _halted_report(exc: MarketHoursHalt) -> PullLegReport:
    """Fold a graceful market-hours drain into a ``halted`` report."""
    _LOG.warning(
        "crossverify.pull_halted_market_hours",
        detail=str(exc),
        action="day deferred — the durable ledger stays resumable",
    )
    return PullLegReport(spot_ok=False, halted=True, halt_reason="market_hours")


def _spot_window(day: date, lookback_days: int) -> tuple[str, str]:
    """The spot chain's ISO-string window (``pull_all_spot`` types ``str | None``)."""
    from_d = day - timedelta(days=lookback_days)
    return from_d.isoformat(), day.isoformat()  # EXPLICIT to_date=day


def run_pull_leg(
    *,
    lake_root: Path,
    day: date,
    lookback_days: int,
    store: InstrumentStore,
    session_rate: SessionRateController,
    market_closed: Callable[[], bool] | None,
    spot_puller: Callable[..., Any] | None = None,
) -> PullLegReport:
    """Run the ONE spot priority pull leg over the whole spot universe.

    The leg rides the injected controller; per-symbol failures ride the
    spine's quarantine (those symbols gate withheld/no-data later); pull-wide
    halts propagate — only :class:`MarketHoursHalt` drains into ``halted``.
    """
    spot_pull = _resolve_spot_puller(spot_puller)
    from_date, to_date = _spot_window(day, lookback_days)
    targets = whole_universe_spot_targets(store)
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
        return _halted_report(exc)
    return PullLegReport(spot_ok=True, halted=False, halt_reason=None)


def run_settle_retry(
    *,
    lake_root: Path,
    day: date,
    lookback_days: int,
    affected: Sequence[CanonicalIdentity],
    store: InstrumentStore,
    session_rate: SessionRateController,
    market_closed: Callable[[], bool] | None,
    spot_puller: Callable[..., Any] | None = None,
) -> PullLegReport:
    """Bounded re-pull of ONLY the *affected* spot symbols (step G2).

    Same call shape as :func:`run_pull_leg` with the target set narrowed to
    the affected SPOT identities (a non-spot identity — structurally absent
    from the spot-only G2 belt — is ignored, never pulled). O(affected).
    """
    spot_pull = _resolve_spot_puller(spot_puller)
    from_date, to_date = _spot_window(day, lookback_days)
    spot_coords = {
        (identity.exchange, identity.lake_symbol)
        for identity in affected
        if identity.lake_segment in ("INDEX", "CASH")
    }
    spot_targets = [
        inst
        for inst in whole_universe_spot_targets(store)
        if (inst.exch_id.value, inst.symbol_name) in spot_coords
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
            return _halted_report(exc)
    return PullLegReport(spot_ok=True, halted=False, halt_reason=None)


__all__ = [
    "PullLegReport",
    "run_pull_leg",
    "run_settle_retry",
    "whole_universe_spot_targets",
]
