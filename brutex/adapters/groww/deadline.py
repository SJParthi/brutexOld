"""Wall-clock freeze guard — the Groww-adapter re-export shim.

DEC-GROWW-PULL-CALL-DEADLINE-001 + DEC-GROWW-TOKEN-REAUTH-001 (MINOR-1). The Groww
``growwapi`` SDK calls carry no inherent wall-clock bound, so a stalled connection
(a half-open socket, a server that never responds) would block its caller FOREVER:

* the historical-pull worker thread (``get_historical_candles``), AND
* the daily 06:00-IST access-token RE-MINT (the lock-holding network mint inside
  :meth:`~brutex.adapters.groww.reauth.SdkRefresher.reauth`).

Both share the same hazard — a hung call with no timeout — and both use the ONE
freeze guard, which now lives at its COMMON home :mod:`brutex.config.deadline`
(DEC-STORE-WRITE-DEADLINE-001: the registry/coverage DuckDB write guard in
:mod:`brutex.data.store_write_deadline` needs the SAME primitive, and ``brutex.data``
must not depend on ``brutex.adapters`` for it — so the stdlib-only primitive moved
DOWN to the config layer both already import, the DEC-GROWW-REAUTH-DEADLINE-001 #910
relocation precedent). This module re-exports it byte-compatibly: every existing
``from brutex.adapters.groww.deadline import DeadlineExceeded, run_with_deadline``
caller (client / reauth / write_deadline / volume_keepalive / movers_warm / tests)
is unchanged, and the exception CLASS is the same object on both paths.
"""

from __future__ import annotations

from brutex.config.deadline import DeadlineExceeded, run_with_deadline

__all__ = ["DeadlineExceeded", "run_with_deadline"]
