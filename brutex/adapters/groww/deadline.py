"""Wall-clock freeze guard — bound an unbounded SDK call by a deadline.

DEC-GROWW-PULL-CALL-DEADLINE-001 + DEC-GROWW-TOKEN-REAUTH-001 (MINOR-1). The Groww
``growwapi`` SDK calls carry no inherent wall-clock bound, so a stalled connection
(a half-open socket, a server that never responds) would block its caller FOREVER:

* the historical-pull worker thread (``get_historical_candles``), AND
* the daily 06:00-IST access-token RE-MINT (the lock-holding network mint inside
  :meth:`~brutex.adapters.groww.reauth.SdkRefresher.reauth`).

Both share the same hazard — a hung call with no timeout. This module is the SINGLE
freeze guard for both. It is deliberately **stdlib-only** (it imports ``threading``
and nothing from the :mod:`brutex.adapters.groww` package), so it can never take part
in an import cycle — the reason it lives here and not in
:mod:`brutex.adapters.groww.client` (a ``client`` import from ``reauth`` would
re-create the client↔reauth cycle CodeQL flagged on #868).
"""

from __future__ import annotations

import threading
from collections.abc import Callable


def run_with_deadline[T](fn: Callable[[], T], seconds: float) -> T:
    """Run *fn* in a daemon thread, raising a timeout if it blows the wall-clock deadline.

    THE FREEZE GUARD (DEC-GROWW-PULL-CALL-DEADLINE-001). The Groww SDK call carries
    no inherent wall-clock bound, so a stalled connection (half-open socket, server
    not responding) would block the caller FOREVER — a retry loop cannot help because
    it only fires on a *raised* error, and a hung call never raises. This caps the
    work: if *fn* has not returned within *seconds*, raise a :class:`TimeoutError`
    whose message carries the ``deadline exceeded`` hint, so
    :func:`~brutex.adapters.groww.errors.from_groww_exception` classifies it as a
    retryable :class:`~brutex.adapters.groww.errors.GrowwTimeoutError`. The caller's
    bounded retry / quarantine path then handles the timeout instead of freezing.
    The abandoned worker is a daemon — it dies with the process and never blocks
    shutdown; its eventual result (if it ever returns) is discarded.

    Generic over the return type so it bounds BOTH the historical candle call (which
    returns a candle ``Mapping``) AND the token re-mint (which returns an SDK tuple) —
    one freeze guard, no copy (CLAUDE.md §11 COMMON).
    """
    result: dict[str, T] = {}
    failure: dict[str, Exception] = {}
    done = threading.Event()

    def _runner() -> None:
        try:
            result["value"] = fn()
        except Exception as exc:  # re-raised on the caller thread below (any error)
            failure["error"] = exc
        finally:
            done.set()

    threading.Thread(target=_runner, name="groww-deadline-call", daemon=True).start()
    if not done.wait(seconds):
        raise TimeoutError(f"Groww call deadline exceeded ({seconds:g}s)")
    if "error" in failure:
        raise failure["error"]
    return result["value"]


__all__ = ["run_with_deadline"]
