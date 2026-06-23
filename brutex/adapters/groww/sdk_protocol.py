"""Groww SDK Protocol — the dependency-free seam BruteX calls the vendor through.

DEC-GROWW-TOKEN-REAUTH-001 / CI cycle-break. The :class:`GrowwSDK` Protocol used to
live in :mod:`brutex.adapters.groww.client`, but :mod:`brutex.adapters.groww.reauth`
imports it AND :mod:`~brutex.adapters.groww.client` imports ``SdkRefresher`` under
``TYPE_CHECKING`` — CodeQL counts the ``TYPE_CHECKING`` edge and flags the bidirectional
``client ↔ reauth`` cycle. Hoisting the Protocol into THIS dependency-free module breaks
the cycle structurally: ``reauth`` imports the Protocol from here (never ``client``), and
``client`` re-exports ``GrowwSDK`` from here (so existing
``from ...client import GrowwSDK`` call sites are unchanged).

This module imports NOTHING from the package (only the standard library), so it can never
participate in an import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class GrowwSDK(Protocol):
    """The subset of the ``growwapi.GrowwAPI`` surface BruteX calls.

    Methods accept ``**_kwargs`` (the real SDK fixes the exact keyword names);
    the client below always calls them with the explicit Groww keyword names.
    """

    def get_historical_candles(self, **_kwargs: object) -> Mapping[str, object]:
        """Return one historical-candle window (row-of-arrays payload)."""
        ...

    def get_expiries(self, **_kwargs: object) -> Mapping[str, object]:
        """Return the FNO expiries for an underlying."""
        ...

    def get_contracts(self, **_kwargs: object) -> Mapping[str, object]:
        """Return the contract symbols for an underlying + expiry."""
        ...


__all__ = ["GrowwSDK"]
