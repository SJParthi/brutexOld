"""ClientPool — K independent Groww SDK connections behind ONE rate limiter + meter.

``DEC-GROWW-CLIENT-POOL-001``. The live spot historical pull fans its windows across a
worker pool (``ingest_spot`` → ``PoolRunParallel``), but every worker funnels its physical
``get_historical_candles`` call through ONE shared :class:`~growwapi.GrowwAPI` SDK socket —
so the workers SERIALISE on that single connection. The achieved-rate meter
(``DEC-GROWW-RATE-ACHIEVED-METER-001``) measured the symptom directly on a live 779-symbol
pull: **avg 0.49 calls in-flight** while the controller allowed 8 r/s, i.e. less than ONE
call overlapping → a measured **~3.4 r/s** ceiling no matter how high the rate controller
was set (Little's Law: throughput = in-flight ÷ latency ≈ 0.49 ÷ 0.15 s ≈ 3.4).

This pool gives each worker thread its OWN :class:`~brutex.adapters.groww.client.GrowwClient`
(its own SDK connection) while keeping the throttle + observability SHARED:

* **ONE** :class:`~brutex.adapters.rate_common.RateController` still paces EVERY physical
  call (the per-window ``pace`` gate + the client's internal retry ``pace``), so the
  AGGREGATE issue rate stays ``<=`` the ceiling — the pool adds *concurrency*, never extra
  rate (CLAUDE.md §2 Rule 6 is untouched: K connections cannot exceed the shared bucket).
* **ONE** shared :class:`~brutex.adapters.call_meter.CallMeter` records every leaf's calls,
  so ``in_flight`` rises toward K and ``effective_rps`` toward the ceiling — and the
  dashboard reads the TRUE aggregate (``adapter.client.meter.snapshot()``), the honest proof
  the speed-up is real and not a claim (CLAUDE.md §2 Rule 1 / Rule 10).

Dispatch is **thread-affinity**: a worker thread is bound to one leaf on its first call
(round-robin, lock-guarded once) and reuses it thereafter (O(1), lock-free fast path via
:class:`threading.local`), so K worker threads spread across K connections AND each thread
keeps its connection warm (HTTP keep-alive). The pool is duck-compatible with
:class:`GrowwClient` (the :class:`~brutex.adapters.groww.client.GrowwClientProtocol`), so it
drops into the adapter's ``client`` slot with ZERO change to the adapter / candle / contract
fetchers.

**Honest envelope (CLAUDE.md §2 Rule 10):** the pull's true sustainable rate is the
VENDOR's, not ours — the live logs show Groww 429s above ~5 r/s (a per-minute cap), so the
pool's realistic win is reaching that vendor ceiling *steadily* (the meter proves the real
number), NOT a fictional sustained 8 r/s. Concurrent use of one TOTP token across K SDK
connections is vendor-UNVERIFIED (ADR-0009 §3); a build failure degrades to a 1-connection
pool (today's exact behaviour) — see :func:`~brutex.adapters.groww.live.build_live_pool`.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.groww.client import GrowwClient


class ClientPool:
    """K independent :class:`GrowwClient` leaves sharing ONE limiter + ONE meter.

    Satisfies :class:`~brutex.adapters.groww.client.GrowwClientProtocol`, so it is a
    drop-in for a single client in the adapter's ``client`` slot. Every leaf MUST share the
    same :class:`~brutex.adapters.call_meter.CallMeter` instance (the constructor enforces
    it) so the achieved-rate gauges aggregate across all connections.

    Parameters
    ----------
    clients
        The leaf clients (``len >= 1``). All must carry the SAME ``meter`` instance — the
        shared aggregate the dashboard reads. :func:`build_live_pool` guarantees this.

    Raises
    ------
    ValueError
        If *clients* is empty, or the leaves do not all share one meter (a misuse that
        would silently split the achieved-rate gauge across connections).
    """

    __slots__ = ("_clients", "_local", "_lock", "_meter", "_next")

    def __init__(self, clients: Sequence[GrowwClient]) -> None:
        leaves = tuple(clients)
        if not leaves:
            raise ValueError("ClientPool needs at least one client")
        meter = leaves[0].meter
        if any(leaf.meter is not meter for leaf in leaves):
            raise ValueError("ClientPool leaves must share ONE CallMeter (aggregate gauge)")
        self._clients = leaves
        self._meter = meter
        #: Per-thread leaf binding (thread-affinity); assigned once, then lock-free.
        self._local = threading.local()
        #: Round-robin cursor for the NEXT new thread's leaf (lock-guarded on assignment).
        self._next = 0
        self._lock = threading.Lock()

    @property
    def meter(self) -> CallMeter:
        """The ONE shared achieved-rate meter (read by the dashboard rate panel)."""
        return self._meter

    @property
    def size(self) -> int:
        """The number of independent SDK connections (leaves) in the pool."""
        return len(self._clients)

    @property
    def self_paces(self) -> bool:
        """Whether EVERY leaf admits its own physical calls through a real ``pace`` hook.

        The pool twin of :attr:`~brutex.adapters.groww.client.GrowwClient.self_paces`
        (DEC-RATE-SINGLE-ADMISSION-001): ``True`` only when ALL leaves carry a bound
        pace hook (:func:`~brutex.adapters.groww.live.build_live_pool` and
        :meth:`with_pace` both bind ONE shared hook into every leaf), so the ingest
        spines drop the per-window admission only when NO connection can bypass the
        shared bucket.
        """
        return all(leaf.self_paces for leaf in self._clients)

    def _leaf(self) -> GrowwClient:
        """Return THIS thread's bound leaf, assigning one round-robin on first use (O(1)).

        The first call from a thread takes the lock once to claim the next round-robin leaf
        (so the first K distinct threads get K DISTINCT connections — the even spread that
        makes the concurrency real); every later call from that thread hits the lock-free
        :class:`threading.local` fast path.
        """
        leaf: GrowwClient | None = getattr(self._local, "leaf", None)
        if leaf is None:
            with self._lock:
                leaf = self._clients[self._next]
                # Advance the round-robin cursor, kept BOUNDED in ``[0, len)`` (never an
                # ever-growing int over a long-lived process — same dispatch, no unbounded
                # counter; per the concurrency hostile review).
                self._next = (self._next + 1) % len(self._clients)
            self._local.leaf = leaf
        return leaf

    def historical_candles(
        self,
        *,
        exchange: str,
        segment: str,
        groww_symbol: str,
        interval: str,
        start: str,
        end: str,
    ) -> Mapping[str, object]:
        """Fetch one historical-candle window on THIS thread's connection."""
        return self._leaf().historical_candles(
            exchange=exchange,
            segment=segment,
            groww_symbol=groww_symbol,
            interval=interval,
            start=start,
            end=end,
        )

    def expiries(
        self,
        *,
        exchange: str,
        underlying_symbol: str,
        year: int | None = None,
        month: int | None = None,
    ) -> Mapping[str, object]:
        """List FNO expiries on THIS thread's connection."""
        return self._leaf().expiries(
            exchange=exchange, underlying_symbol=underlying_symbol, year=year, month=month
        )

    def contracts(
        self, *, exchange: str, underlying_symbol: str, expiry_date: str
    ) -> Mapping[str, object]:
        """List contract symbols on THIS thread's connection."""
        return self._leaf().contracts(
            exchange=exchange, underlying_symbol=underlying_symbol, expiry_date=expiry_date
        )

    def with_pace(self, pace: Callable[[], None]) -> ClientPool:
        """Return a new pool with *pace* rebound on EVERY leaf (shared meter preserved).

        The :class:`~brutex.adapters.groww.client.GrowwClientProtocol` seam
        (:func:`~brutex.adapters.groww.run_ingest.with_paced_client`) wires the shared rate
        bucket into the client's internal fast-retry AFTER the adapter is built; for a pool
        that must re-fan into ALL K leaves, not just one. Each leaf's ``with_pace`` is a
        :func:`dataclasses.replace` that keeps the shared ``meter`` (``compare=False``), so
        the rebuilt pool still aggregates into the same meter. Thread bindings reset (the
        fresh pool rebinds on first use) — harmless, the leaves are equivalent up to ``pace``.
        """
        return ClientPool([leaf.with_pace(pace) for leaf in self._clients])


__all__ = ["ClientPool"]
