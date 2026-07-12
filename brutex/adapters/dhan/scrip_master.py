"""Dhan scrip-master securityId resolution — O(1) ``{symbol → securityId}``, honest-degrade.

DEC-DHAN-HONEST-DEGRADE-001 (ADR-0022). Dhan addresses an instrument by a numeric
``securityId``; the LAW (VERIFIED-live: SDK ``_security.py`` + the openalgo pattern)
is to resolve it at RUNTIME from the compact scrip-master CSV, never to hardcode ids
blindly. This module resolves the index symbols (NIFTY / BANKNIFTY / SENSEX / INDIA
VIX) to their ``IDX_I`` securityIds with three tiers:

1. **Live scrip-master** — a best-effort fetch+parse of ``api-scrip-master.csv``.
2. **In-repo-verified fallback** (:data:`~brutex.adapters.dhan.constants.\
DHAN_FALLBACK_SECURITY_IDS`) — NIFTY 13 (official), BANKNIFTY 25 / SENSEX 51 /
   INDIA VIX 21 (in-repo-verified) — used when the live map lacks a symbol / the
   fetch fails.
3. **Honest omission** — a symbol resolvable by NEITHER is OMITTED from the result
   (feeds the loud-skip path in the ingest spine — a re-pullable PLANNED window,
   zero bars), NEVER a blind / fabricated id (Golden Rule #1).

The live fetch+parse is ``# pragma: no cover``: the CSV host is egress-blocked
in-sandbox and its exact column schema is UNVERIFIED, so it is flipped on with a
credentialed probe transcript (§6). The RESOLUTION + FALLBACK logic is fully tested
via an injected ``live_resolver`` (a fake), and the verified fallback ids resolve
every index today with no network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import structlog

from brutex.adapters.dhan.constants import (
    DHAN_FALLBACK_SECURITY_IDS,
    SCRIP_MASTER_URL,
)

_LOG = structlog.get_logger("brutex.adapters.dhan.scrip_master")

#: A live securityId resolver: returns a ``{symbol → securityId}`` map, or raises on
#: an unavailable / malformed source (the resolver degrades to the fallback map).
LiveIdResolver = Callable[[], Mapping[str, int]]


@dataclass(frozen=True, slots=True)
class ScripResolution:
    """The outcome of resolving a set of index symbols to Dhan securityIds.

    Attributes
    ----------
    security_ids
        ``{symbol → securityId}`` for every RESOLVED symbol (live wins, else fallback).
    unresolved
        Symbols resolvable by NEITHER the live map NOR the fallback — OMITTED from
        :attr:`security_ids` (the loud-skip path; never a blind id).
    used_live
        ``True`` iff a live resolver was consulted AND succeeded (a fetch failure or
        no resolver leaves the fallback map as the sole source).
    """

    security_ids: Mapping[str, int]
    unresolved: tuple[str, ...]
    used_live: bool


def resolve_security_ids(
    symbols: Sequence[str],
    *,
    live_resolver: LiveIdResolver | None = None,
    fallback: Mapping[str, int] = DHAN_FALLBACK_SECURITY_IDS,
) -> ScripResolution:
    """Resolve *symbols* to Dhan securityIds — live map, else fallback, else omit.

    Parameters
    ----------
    symbols
        The index symbols to resolve (e.g. the Dhan spot index set).
    live_resolver
        Optional best-effort live ``{symbol → securityId}`` source. ANY exception it
        raises is caught + logged (``dhan.scrip_master.fetch_failed``) and the
        fallback map takes over — a transient CSV outage never blocks the pull. ``None``
        (the tested default) uses the fallback map alone.
    fallback
        The verified ``{symbol → securityId}`` map (default the in-repo-verified ids).

    Returns
    -------
    ScripResolution
        The resolved ids, the honestly-omitted unresolved symbols, and whether a live
        map was used.
    """
    live_map: Mapping[str, int] = {}
    used_live = False
    if live_resolver is not None:
        try:
            live_map = dict(live_resolver())
            used_live = True
        except Exception as exc:  # pylint: disable=broad-exception-caught — best-effort: fall back on ANY failure
            _LOG.warning(
                "dhan.scrip_master.fetch_failed",
                url=SCRIP_MASTER_URL,
                error_type=type(exc).__name__,
            )
    resolved: dict[str, int] = {}
    unresolved: list[str] = []
    for symbol in symbols:
        security_id = live_map.get(symbol)
        source = "live"
        if security_id is None:
            security_id = fallback.get(symbol)
            source = "fallback"
        if security_id is None:
            unresolved.append(symbol)
            _LOG.warning("dhan.scrip_master.unresolved", symbol=symbol)
            continue
        resolved[symbol] = security_id
        _LOG.debug("dhan.scrip_master.resolved", symbol=symbol, source=source)
    return ScripResolution(security_ids=resolved, unresolved=tuple(unresolved), used_live=used_live)


def fetch_live_security_ids() -> Mapping[
    str, int
]:  # pragma: no cover — network + UNVERIFIED CSV schema
    """Fetch + parse the compact scrip-master CSV into ``{symbol → securityId}`` (index rows).

    The production live resolver: fetches :data:`~brutex.adapters.dhan.constants.\
SCRIP_MASTER_URL` and parses the ``IDX_I`` index rows. The CSV host is egress-blocked
    in-sandbox and its exact column schema is UNVERIFIED, so this whole path is
    ``# pragma: no cover`` — flipped on with a credentialed probe transcript (§6);
    :func:`resolve_security_ids` drives its output through the tested fallback logic.
    """
    import csv
    import io
    import urllib.request

    with urllib.request.urlopen(SCRIP_MASTER_URL, timeout=30) as response:  # noqa: S310 — fixed HTTPS host
        text = response.read().decode("utf-8", errors="replace")
    result: dict[str, int] = {}
    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        segment = str(row.get("SEM_SEGMENT", "")).strip().upper()
        if segment != "I":  # index segment
            continue
        symbol = str(row.get("SEM_TRADING_SYMBOL", "")).strip().upper()
        raw_id = str(row.get("SEM_SMST_SECURITY_ID", "")).strip()
        if symbol and raw_id.isdigit():
            result[symbol] = int(raw_id)
    return result


__all__ = [
    "LiveIdResolver",
    "ScripResolution",
    "fetch_live_security_ids",
    "resolve_security_ids",
]
