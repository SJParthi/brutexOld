"""Dhan client pool — K connections sharing one meter, round-robin dispatch.

DEC-DHAN-ADAPTER-001. A :class:`DhanClientPool` fans requests across K
:class:`~brutex.adapters.dhan.client.DhanClient` leaves that share ONE
:class:`~brutex.adapters.call_meter.CallMeter` (so the dashboard rate panel reports an
aggregate) and, when wired, one refresher. It satisfies the same
:class:`~brutex.adapters.dhan.client.DhanClientProtocol` as a single client, so the
adapter is identical for both.

**Honest envelope (Golden Rule #10):** the Data API is a low-concurrency 5/s surface,
so the default pool is small. Round-robin dispatch is thread-safe (a lock guards the
cursor). **INERT / reference-only** — nothing production wires a live transport.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence

from brutex.adapters.call_meter import CallMeter
from brutex.adapters.dhan.client import DhanClient

#: Default pool size — small, matching the 5/s Data-API surface.
DEFAULT_POOL_SIZE: int = 2


class DhanClientPool:
    """A pool of K :class:`DhanClient` leaves sharing one meter; round-robin dispatch."""

    __slots__ = ("_clients", "_cursor", "_lock", "_meter")

    def __init__(self, clients: Sequence[DhanClient]) -> None:
        """Build a pool over *clients* (all sharing the first leaf's meter).

        Raises :class:`ValueError` for an empty pool (fail-loud — never a pool with no
        connection to dispatch through).
        """
        if not clients:
            raise ValueError("DhanClientPool requires at least one client")
        self._meter = clients[0].meter
        # Rebind every leaf onto the shared meter so the aggregate is honest.
        self._clients: tuple[DhanClient, ...] = tuple(
            _with_meter(client, self._meter) for client in clients
        )
        self._cursor = 0
        self._lock = threading.Lock()

    @property
    def meter(self) -> CallMeter:
        """The shared aggregate achieved-throughput meter."""
        return self._meter

    @property
    def size(self) -> int:
        """The number of connections in the pool."""
        return len(self._clients)

    def _next(self) -> DhanClient:
        """Return the next leaf (round-robin, thread-safe)."""
        with self._lock:
            client = self._clients[self._cursor]
            self._cursor = (self._cursor + 1) % len(self._clients)
        return client

    def historical_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
        expiry_code: int | None = None,
    ) -> Mapping[str, object]:
        """Dispatch a daily historical fetch to the next leaf."""
        return self._next().historical_candles(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument=instrument,
            from_date=from_date,
            to_date=to_date,
            oi=oi,
            expiry_code=expiry_code,
        )

    def intraday_candles(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument: str,
        interval: str,
        from_date: str,
        to_date: str,
        oi: bool = False,
    ) -> Mapping[str, object]:
        """Dispatch an intraday fetch to the next leaf."""
        return self._next().intraday_candles(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument=instrument,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            oi=oi,
        )

    def option_chain(
        self, *, underlying_scrip: int, underlying_seg: str, expiry: str
    ) -> Mapping[str, object]:
        """Dispatch an option-chain fetch to the next leaf."""
        return self._next().option_chain(
            underlying_scrip=underlying_scrip, underlying_seg=underlying_seg, expiry=expiry
        )

    def expiry_list(self, *, underlying_scrip: int, underlying_seg: str) -> Mapping[str, object]:
        """Dispatch an expiry-list fetch to the next leaf."""
        return self._next().expiry_list(
            underlying_scrip=underlying_scrip, underlying_seg=underlying_seg
        )

    def rolling_option(
        self,
        *,
        exchange_segment: str,
        security_id: str,
        instrument: str,
        expiry_flag: str,
        expiry_code: int,
        strike: str,
        drv_option_type: str,
        interval: str,
        from_date: str,
        to_date: str,
        required_data: Sequence[str],
    ) -> Mapping[str, object]:
        """Dispatch a rolling-option fetch to the next leaf."""
        return self._next().rolling_option(
            exchange_segment=exchange_segment,
            security_id=security_id,
            instrument=instrument,
            expiry_flag=expiry_flag,
            expiry_code=expiry_code,
            strike=strike,
            drv_option_type=drv_option_type,
            interval=interval,
            from_date=from_date,
            to_date=to_date,
            required_data=required_data,
        )

    def with_pace(self, pace: Callable[[], None]) -> DhanClientPool:
        """Return a pool with every leaf bound to *pace* (sharing this pool's meter)."""
        return DhanClientPool([client.with_pace(pace) for client in self._clients])

    def with_rate_hooks(
        self,
        *,
        pace: Callable[[], None],
        on_rate_limit: Callable[[], None],
        on_success: Callable[[], None],
        option_chain_pace: Callable[[], None] | None = None,
    ) -> DhanClientPool:
        """Return a pool with every leaf bound to the rate-feedback hooks."""
        return DhanClientPool(
            [
                client.with_rate_hooks(
                    pace=pace,
                    on_rate_limit=on_rate_limit,
                    on_success=on_success,
                    option_chain_pace=option_chain_pace,
                )
                for client in self._clients
            ]
        )


def _with_meter(client: DhanClient, meter: CallMeter) -> DhanClient:
    """Return *client* rebound onto the shared *meter* (a dataclass replace)."""
    from dataclasses import replace

    return replace(client, meter=meter)


__all__ = ["DEFAULT_POOL_SIZE", "DhanClientPool"]
