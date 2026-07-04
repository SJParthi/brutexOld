"""Authoritative per-``(underlying, expiry)`` strike chains from ``registry.duckdb``.

DEC-MONEYNESS-REGISTRY-STEP-001 — the ONE loader (CLAUDE.md §11 COMMON) both display
surfaces share: the ``/db`` F&O candle page (:mod:`brutex.dashboard.db_view`) and the
options-movers screen (:mod:`brutex.dashboard.movers_options`) derive the ATM±N
strike-grid step from the strike ladder of an ``(underlying, expiry)``. The
lake-observed ladder is SPARSE (only strikes whose candles were pulled), so its min
positive gap can OVERESTIMATE the true base step by an integer factor. The FULL
vendor-listed chain already lives at read time in the metadata-pull registry
(:meth:`~brutex.data.registry_store.RegistryStore.list_contracts`, populated from the
vendor ``get_contracts`` — independent of which strikes' candles were pulled), so this
module resolves the AUTHORITATIVE ladders from there.

GRACEFUL FALLBACK (never break a CLI-only-pulled lake — Golden Rule #1, the exact
:func:`~brutex.dashboard.movers_options.load_registry_expiries` pattern of
DEC-MOVERS-CALENDAR-TIERS-001): an ABSENT registry file (a lake pulled only with
``brutex fetch``), an EMPTY one (the metadata phase never ran), or an UNREADABLE one
(a stale / version-incompatible cache raising
:class:`~brutex.data.registry_store.RegistryError`) yields ``None`` — the caller then
keeps today's lake-observed step, labelled low-confidence
(:attr:`~brutex.data.moneyness.StepConfidence.OBSERVED`), never a fabricated step and
never a 500.

HONESTY (Golden Rule #1/#10): "authoritative" means *the vendor-listed chain as of the
last metadata pull* — whether the live vendor chain itself is complete is vendor
behaviour, UNVERIFIED from this repo. Registry underlying tokens are matched verbatim
against the lake-parsed underlying; a naming mismatch degrades gracefully to the
observed ladder (nothing stronger is claimed).

This module imports only :mod:`brutex.data.*` so neither dashboard consumer has to
import the other (no ``db_view`` ↔ ``movers_options`` cycle).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol, TypeVar

import structlog

from brutex.data.moneyness import OPTION_CALL, OPTION_PUT
from brutex.data.registry_store import ContractRow, RegistryError, RegistryStore

_LOG = structlog.get_logger(__name__)

__all__ = [
    "StrikeChainCache",
    "StrikeChains",
    "load_registry_strike_chain",
    "load_registry_strike_chains",
    "make_registry_chain_loader",
    "registry_chain_key",
]

_T = TypeVar("_T")

#: ``(underlying, expiry) → the full vendor-listed option strike ladder`` — the shape
#: :func:`load_registry_strike_chains` returns and the step resolvers consume.
StrikeChains = dict[tuple[str, date], tuple[Decimal, ...]]

#: The two option sides that carry a strike (a ``FUT`` leg has none and is excluded).
_OPTION_SIDES: Final[frozenset[str]] = frozenset({OPTION_CALL, OPTION_PUT})


def _option_strike(row: ContractRow) -> Decimal | None:
    """The row's strike as :class:`~decimal.Decimal`, or ``None`` for a non-option leg.

    The registry stores ``strike`` as a ``float`` (``ContractRow.strike``); the
    moneyness engine's :func:`~brutex.data.moneyness.derive_strike_step` accepts ONLY
    :class:`~decimal.Decimal` (its ``_is_positive_finite`` guard silently drops any
    other type), so the ``Decimal(str(...))`` conversion here is REQUIRED — junk
    values (``NaN`` / ``inf`` / ``<= 0``) still convert cleanly and are then rejected
    by the engine's own positive-finite filter, never raising.
    """
    if row.side not in _OPTION_SIDES or row.strike is None:
        return None
    return Decimal(str(row.strike))


def load_registry_strike_chains(
    registry_path: Path,
    *,
    exchange: str | None = None,
    underlying: str | None = None,
    expiry: date | None = None,
) -> StrikeChains | None:
    """Read the FULL vendor option strike chains from ``registry.duckdb``, or ``None``.

    Groups every persisted CE/PE contract leg by ``(underlying, expiry_date)`` and
    returns each group's strike ladder — the AUTHORITATIVE grid the display step
    derivation prefers over the sparse lake-observed ladder
    (DEC-MONEYNESS-REGISTRY-STEP-001). Optional *exchange* / *underlying* / *expiry*
    filters narrow the read — BOTH production surfaces pass all three via
    :func:`load_registry_strike_chain` (one ``(underlying, expiry)`` chain per read;
    the whole-registry form exists for callers that genuinely need every chain, but
    no dashboard surface does — Golden Rule #5 bounded reads).

    GRACEFUL FALLBACK (Golden Rule #1): ``None`` — never an exception — when the
    registry is ABSENT, EMPTY (no matching option legs), or UNREADABLE (a
    :class:`~brutex.data.registry_store.RegistryError` is logged as
    ``moneyness.registry_strikes_unreadable`` and swallowed), so the caller degrades
    to the lake-observed step with its honest low-confidence label.

    Parameters
    ----------
    registry_path
        The ``<lake_root>/registry.duckdb`` path
        (:func:`~brutex.dashboard.db_view.registry_path_for`).
    exchange
        Restrict to one exchange (``NSE`` / ``BSE``), or ``None`` for all.
    underlying, expiry
        Restrict to one underlying / one expiry date, or ``None`` for all.

    Returns
    -------
    StrikeChains | None
        ``(underlying, expiry) → the vendor strike ladder`` (unordered; the step
        deriver sorts), or ``None`` when no authoritative chain is available.
    """
    if not registry_path.exists():
        return None
    try:
        with RegistryStore.open(registry_path) as registry:
            rows = registry.list_contracts(exch=exchange, underlying=underlying, expiry_date=expiry)
    except RegistryError as exc:
        # A stale / version-incompatible / locked registry must never 500 the page —
        # degrade to the lake-observed step derivation (Golden Rule #1 / #10).
        _LOG.warning("moneyness.registry_strikes_unreadable", error=str(exc))
        return None
    chains: dict[tuple[str, date], list[Decimal]] = {}
    for row in rows:
        strike = _option_strike(row)
        if strike is None:
            continue
        chains.setdefault((row.underlying, row.expiry_date), []).append(strike)
    if not chains:
        return None
    return {key: tuple(strikes) for key, strikes in chains.items()}


def load_registry_strike_chain(
    registry_path: Path,
    *,
    exchange: str | None,
    underlying: str,
    expiry: date,
) -> tuple[Decimal, ...] | None:
    """Read exactly ONE ``(underlying, expiry)`` strike chain — never the whole registry.

    The BOUNDED single-chain read (Golden Rule #5 / DEC-FNO-HONESTY-PACK-001) BOTH
    display surfaces share: the ``/db`` page (``db_view._registry_strike_chain``) and
    the options-movers loader (:func:`make_registry_chain_loader`) each need only the
    chains of their own tier-target keys, so the DuckDB read is narrowed to one
    ``(exchange, underlying, expiry)`` via :meth:`~brutex.data.registry_store.
    RegistryStore.list_contracts`'s SQL filters — O(one chain), never an
    O(entire-registry-history) materialisation held resident. Delegates to
    :func:`load_registry_strike_chains` (the ONE read + graceful-fallback source of
    truth, CLAUDE.md §11 COMMON) and inherits its absent/empty/``RegistryError`` →
    ``None`` triad.
    """
    chains = load_registry_strike_chains(
        registry_path, exchange=exchange, underlying=underlying, expiry=expiry
    )
    if chains is None:
        return None
    return chains.get((underlying, expiry))


class StrikeChainCache(Protocol):
    """The minimal generation-stamped cache contract the chain loader needs.

    Structural (``Protocol``) so this module keeps importing only ``brutex.data``
    plus stdlib (no ``db_view`` ↔ ``movers_options`` ↔ ``movers_cache`` coupling) —
    the dashboard injects the COMMON generation-stamped LRU
    (:class:`brutex.dashboard.movers_cache.MoversSnapshotCache`), which satisfies
    this — the exact ``db_view.CandleCountCache`` shape. A ``compute`` that raises
    propagates cleanly and nothing is cached.
    """

    def get_or_compute(self, key: str, *, generation: int, compute: Callable[[], _T]) -> _T:
        """Return *key*'s cached value for *generation*, running *compute* on a miss."""
        raise NotImplementedError  # pragma: no cover — Protocol stub, never executed


def registry_chain_key(exchange: str | None, underlying: str, expiry: date) -> str:
    """Build the stable cache key for one movers registry strike chain.

    The chain is a property of ``(exchange, underlying, expiry)`` alone — invariant to
    the snapshot date / time / interval / tier — so every options-poll coordinate that
    lands on the same tier-target key shares ONE cached chain per write-generation (a
    metadata pull bumps the generation → re-read, never stale — Golden Rule #1). The
    ``options_strikes|`` prefix keeps the pre-existing per-exchange namespace of the
    dedicated ``_options_strikes_cache``; ``None`` exchange (the "all exchanges"
    coordinate) keys as an empty segment. ``|`` cannot appear in an exchange token, an
    underlying symbol, or an ISO date, so the join is unambiguous.
    """
    return f"options_strikes|{exchange or ''}|{underlying}|{expiry.isoformat()}"


def make_registry_chain_loader(
    registry_path: Path,
    *,
    exchange: str | None,
    cache: StrikeChainCache,
    generation: int,
) -> Callable[[str, date], tuple[Decimal, ...] | None]:
    """Build the LAZY per-``(underlying, expiry)`` chain loader the movers screen threads.

    The bounded-read fix (Golden Rule #5, DEC-FNO-HONESTY-PACK-001): the options
    snapshot consults only its handful of tier-target ``(underlying, expiry)`` keys,
    so the loader reads ONE narrowed chain per key
    (:func:`load_registry_strike_chain`) — memoised per key + write-generation behind
    the injected *cache* (mirroring the ``/db`` ``_registry_strike_chain`` shape) —
    instead of materialising the ENTIRE monotonically-growing contracts table per
    generation. Deliberately NOT filtered by ``expiry >= today``: the movers ``date=``
    param serves historical snapshots, so any expiry the snapshot targets is loadable.
    The ONE builder both the ``/api/v1/movers`` route and the post-pull warm use
    (CLAUDE.md §11 COMMON — the warmed candidate set stays byte-identical to a live
    poll's). An absent/empty/unreadable registry yields ``None`` per key → the honest
    ``observed`` low-confidence fallback.
    """

    def _chain_for(underlying: str, expiry: date) -> tuple[Decimal, ...] | None:
        return cache.get_or_compute(
            registry_chain_key(exchange, underlying, expiry),
            generation=generation,
            compute=lambda: load_registry_strike_chain(
                registry_path, exchange=exchange, underlying=underlying, expiry=expiry
            ),
        )

    return _chain_for
