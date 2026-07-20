"""Dhan index securityId resolution — O(1) ``{symbol → securityId}`` from a hardcoded map.

DEC-DHAN-HONEST-DEGRADE-001 (ADR-0022). Dhan addresses an instrument by a numeric
``securityId``. This module resolves the index symbols (NIFTY / BANKNIFTY / SENSEX /
INDIA VIX) to their ``IDX_I`` securityIds from the HARDCODED verified-pending map
:data:`~brutex.adapters.dhan.constants.DHAN_INDEX_SECURITY_IDS` — NIFTY 13 (official),
BANKNIFTY 25 / SENSEX 51 / INDIA VIX 21 (in-repo-verified) — with a fail-loud honest
omission for anything the map does not cover:

1. **Hardcoded verified-pending map** — the single source of truth (no network).
2. **Honest omission** — a symbol ABSENT from the map is OMITTED from the result
   (feeds the loud-skip path in the ingest spine — a re-pullable PLANNED window,
   zero bars), NEVER a blind / fabricated id (Golden Rule #1).

The IDs are hardcoded verified-PENDING constants: the read-only probe kit
(``docs/runbooks/feed-probe/probe_feeds.py``) is the ONE-TIME ID cross-check — it
fetches the public compact scrip-master CSV and confirms each hardcoded id against
it. A runtime live-CSV loader is deliberately NOT carried here (coordinator ruling):
dynamic securityId resolution is a scope-expansion concern for a later, dedicated PR,
not this standalone SPOT-index track.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import structlog

from brutex.adapters.dhan.constants import DHAN_INDEX_SECURITY_IDS

_LOG = structlog.get_logger("brutex.adapters.dhan.scrip_master")


@dataclass(frozen=True, slots=True)
class ScripResolution:
    """The outcome of resolving a set of index symbols to Dhan securityIds.

    Attributes
    ----------
    security_ids
        ``{symbol → securityId}`` for every RESOLVED symbol (from the hardcoded map).
    unresolved
        Symbols ABSENT from the hardcoded map — OMITTED from :attr:`security_ids`
        (the loud-skip path; never a blind id).
    """

    security_ids: Mapping[str, int]
    unresolved: tuple[str, ...]


def resolve_security_ids(
    symbols: Sequence[str],
    *,
    security_id_map: Mapping[str, int] = DHAN_INDEX_SECURITY_IDS,
) -> ScripResolution:
    """Resolve *symbols* to Dhan securityIds from the hardcoded map, else honest omit.

    Parameters
    ----------
    symbols
        The index symbols to resolve (e.g. the Dhan spot index set).
    security_id_map
        The verified ``{symbol → securityId}`` map (default the in-repo verified-pending
        ids). A symbol ABSENT from it is honestly omitted (never a fabricated id).

    Returns
    -------
    ScripResolution
        The resolved ids and the honestly-omitted unresolved symbols.
    """
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for symbol in symbols:
        security_id = security_id_map.get(symbol)
        if security_id is None:
            unresolved.append(symbol)
            _LOG.warning("dhan.scrip_master.unresolved", symbol=symbol)
            continue
        resolved[symbol] = security_id
        _LOG.debug("dhan.scrip_master.resolved", symbol=symbol)
    return ScripResolution(security_ids=resolved, unresolved=tuple(unresolved))


__all__ = [
    "ScripResolution",
    "resolve_security_ids",
]
