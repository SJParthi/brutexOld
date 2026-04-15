"""Groww broker adapter — BruteX's sole L1 adapter for v1.

Only two Groww SDK surfaces are used:

* ``growwapi.GrowwAPI.get_all_instruments`` / ``get_instrument_by_groww_symbol``
  (instruments CSV + lookups)
* ``growwapi.GrowwAPI.get_expiries`` / ``get_contracts`` /
  ``get_historical_candles`` (backtesting endpoints)

No live feed, no orders, no portfolio, no margin-live. See
``docs/groww-sdk/`` for the authoritative SDK reference captured verbatim
from the Groww docs site.
"""

from __future__ import annotations

__all__: list[str] = []
