"""Front-month futures contract resolution from the REAL registry calendar (R14).

DEC-FUTURES-EXEC-TRANSLATION-001: the executor's production
:class:`~brutex.futures.executor.FrontMonthResolver` seam. The front-month
contract for a trade is the FUT contract with the EARLIEST expiry ``>=`` the
trade's entry IST date, selected over the vendor-listed expiry calendar the
metadata pull persisted into ``registry.duckdb`` — **never a computed
last-Thursday formula** (a holiday-shifted expiry arrives verbatim from the
vendor's ``get_expiries`` into ``expiry_date`` + the symbol's ``DDMmmYY`` token;
the resolver only ever COMPARES real dates). The selection delegates to the ONE
shared front-month picker
:func:`brutex.dashboard.movers_futures.select_near_month` (earliest expiry
``>= on_date``, ties on the symbol string — deterministic, never
liquidity-picked; CLAUDE.md §11 COMMON).

## Graceful fallback (Golden Rule #1)

A CLI-only lake seeds no registry. An absent / empty / unreadable
``registry.duckdb`` (or one with no FUT rows for the underlying) falls back to
LAKE-PARTITION DISCOVERY: the ``bars/<exch>/FNO/`` contract directories are
scanned once and decoded via the canonical
:func:`~brutex.data.fno_symbol.parse_fno_symbol` (the registry
``contract_symbol`` and the on-disk directory name are the SAME string — both
are the lake partition key). Still nothing → ``None`` → the executor's honest
``NO_CONTRACT`` reject. Nothing is ever synthesized.

## O(1) story (Golden Rule #10)

The per-underlying ``(contract_symbol, expiry)`` calendar loads ONCE and is
cached; each per-trade resolve is then one :func:`select_near_month` pass over
the ≤ ~3 live serial months — O(k log k) over a tiny fixed k, effectively O(1)
per trade, honestly NOT a literal O(1). The FIRST load is O(contracts) (one
registry query or one directory scan) — stated, never hidden.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from brutex.costs.futures_costs import futures_exchange_for_underlying
from brutex.data.fno_symbol import parse_fno_symbol

if TYPE_CHECKING:  # pragma: no cover — annotation-only import
    from collections.abc import Iterable

__all__ = ["FutContract", "RegistryFrontMonthResolver", "resolve_front_month"]

_LOG = structlog.get_logger(__name__)

#: The instrument-master filename the registry sits beside (CLAUDE.md §6 auth
#: table: ``<lake_root>/instruments.duckdb``); ``db_view.registry_path_for``
#: resolves the SIBLING ``registry.duckdb`` from it.
_INSTRUMENTS_DB_NAME = "instruments.duckdb"

#: The F&O lake segment directory (DEC-FNO-CANDLE-LAKE-001).
_FNO_SEGMENT = "FNO"


@dataclass(frozen=True, slots=True)
class FutContract:
    """One live-or-listed FUT contract: its lake key + its REAL expiry date.

    Attributes
    ----------
    contract_symbol
        The full contract ``groww_symbol`` (``NSE-NIFTY-30Sep25-FUT``) — the
        registry row's ``contract_symbol`` AND the lake partition directory
        name (the same string).
    expiry_date
        The vendor-listed (holiday-shifted, real) expiry date.
    """

    contract_symbol: str
    expiry_date: date

    def __post_init__(self) -> None:
        if not self.contract_symbol.strip():
            raise ValueError("FutContract.contract_symbol must be non-blank")


def resolve_front_month(contracts: Iterable[FutContract], *, on_date: date) -> str | None:
    """Pick the front-month contract symbol as of *on_date*, or ``None``.

    Delegates to the SHARED
    :func:`brutex.dashboard.movers_futures.select_near_month` — the single
    front-month source (earliest expiry ``>= on_date``; a contract expiring
    today is still live through its session; ties break on the symbol string —
    deterministic, Golden Rule #7; never liquidity-picked, R14).

    Parameters
    ----------
    contracts
        One underlying's FUT contracts (any order; duplicates harmless — the
        picker sorts).
    on_date
        The trade's entry IST date.

    Returns
    -------
    str | None
        The front-month ``contract_symbol``, or ``None`` when every contract
        has expired (the executor's honest ``NO_CONTRACT``).
    """
    # Lazy import: keeps ``brutex.futures`` import-light — the movers module
    # pulls the dashboard read stack, only needed once resolution actually runs.
    from brutex.dashboard.movers_futures import select_near_month

    items = [(c.contract_symbol, c.expiry_date) for c in contracts]
    return select_near_month(items, on_date=on_date)


class RegistryFrontMonthResolver:
    """Production :class:`~brutex.futures.executor.FrontMonthResolver` over the registry.

    Construct with the lake root; inject into
    :func:`~brutex.futures.executor.materialize_futures_trade` /
    :func:`~brutex.futures.executor.execute_futures_sim`. The per-underlying FUT
    calendar is loaded once (registry first, lake-discovery fallback) and
    cached, so a whole run pays the load a single time per underlying.
    """

    def __init__(self, lake_root: Path) -> None:
        self._lake_root = lake_root
        # (exchange, underlying) → the cached FUT calendar (possibly empty).
        self._calendar: dict[tuple[str, str], tuple[FutContract, ...]] = {}

    def resolve(self, *, instrument: str, on_date: date) -> str | None:
        """Return the front-month FUT contract symbol for *instrument* as of *on_date*.

        ``None`` when neither the registry nor the lake lists a live contract —
        the executor then rejects ``NO_CONTRACT``, never fabricates (R14).
        """
        exchange = futures_exchange_for_underlying(instrument)
        key = (exchange, instrument)
        calendar = self._calendar.get(key)
        if calendar is None:
            calendar = self._load_calendar(exchange, instrument)
            self._calendar[key] = calendar
        return resolve_front_month(calendar, on_date=on_date)

    def _load_calendar(self, exchange: str, underlying: str) -> tuple[FutContract, ...]:
        """Load one underlying's FUT calendar: registry first, lake fallback.

        The registry read is guarded — an absent file skips straight to the
        fallback; a read failure (``RegistryError``) WARNs and falls back
        (Golden Rule #1: a broken sidecar cache never breaks resolution while
        the lake itself can answer).
        """
        registry = self._registry_contracts(exchange, underlying)
        if registry:
            return registry
        return self._lake_discovered_contracts(exchange, underlying)

    def _registry_contracts(self, exchange: str, underlying: str) -> tuple[FutContract, ...]:
        """Read the underlying's FUT rows from ``registry.duckdb`` (may be empty)."""
        # Lazy imports: db_view is the ONE registry-path resolver (CLAUDE.md
        # §11 COMMON — verified db_view.py:4864 takes the instruments.duckdb
        # store path and returns its SIBLING registry.duckdb); registry_store
        # owns the DuckDB read. Both are deliberately not module-level so
        # importing brutex.futures stays light.
        from brutex.dashboard.db_view import registry_path_for
        from brutex.data.registry_store import RegistryError, open_registry

        registry_path = registry_path_for(self._lake_root / _INSTRUMENTS_DB_NAME)
        if not registry_path.exists():
            return ()
        try:
            with open_registry(registry_path) as store:
                rows = store.list_contracts(exch=exchange, underlying=underlying)
        except RegistryError as exc:
            _LOG.warning(
                "futures_resolver.registry_error",
                registry_path=str(registry_path),
                exchange=exchange,
                underlying=underlying,
                error=str(exc),
            )
            return ()
        return tuple(
            FutContract(contract_symbol=row.contract_symbol, expiry_date=row.expiry_date)
            for row in rows
            if row.side == "FUT"
        )

    def _lake_discovered_contracts(self, exchange: str, underlying: str) -> tuple[FutContract, ...]:
        """Discover FUT contracts from the lake's ``bars/<exch>/FNO/`` directories.

        Mirrors the options lake source's discovery walk (``sorted()`` for a
        deterministic filesystem-independent order): each directory name is
        decoded via the canonical :func:`parse_fno_symbol`; only this
        underlying's FUT contracts are kept; junk names are skipped honestly.
        """
        fno_dir = self._lake_root / "bars" / exchange / _FNO_SEGMENT
        if not fno_dir.is_dir():
            return ()
        contracts: list[FutContract] = []
        for contract_dir in sorted(fno_dir.iterdir()):
            if not contract_dir.is_dir():
                continue
            parts = parse_fno_symbol(contract_dir.name)
            if parts is None or parts.instrument_type != "FUT":
                continue
            if parts.underlying != underlying:
                continue
            contracts.append(
                FutContract(contract_symbol=contract_dir.name, expiry_date=parts.expiry)
            )
        return tuple(contracts)
