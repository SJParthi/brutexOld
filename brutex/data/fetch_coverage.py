"""Shared fetch-window identity — the immutable :class:`WindowKey`.

Once the home of the ``FetchCoverage`` skip-set, this module now holds ONLY the
shared :class:`WindowKey` — the immutable, hashable identity of one fetch window
(one vendor API call). The coverage set itself was RETIRED in
DEC-WINDOW-LEDGER-001 Stage e: the unified
:class:`~brutex.data.window_ledger.WindowLedger` is the SOLE skip authority, so
the coarse span-enclosure coverage set (and its persistence sidecar) are gone.

:class:`WindowKey` stays here because it is the COMMON key type many modules
import (``from brutex.data.fetch_coverage import WindowKey``): the ledger, the
status log, and both the spot + option ingest paths all key their per-window
records by it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class WindowKey:
    """One fetch window's identity — the unit of a single vendor API call.

    Immutable + hashable, so it is an O(1) set/dict member. ``market`` namespaces
    the data source (e.g. ``"groww"``); ``security_id`` + ``interval`` +
    ``(start, end)`` pin the exact window the adapter would fetch.
    """

    market: str
    security_id: str
    interval: str
    start: date
    end: date


__all__ = ["WindowKey"]
