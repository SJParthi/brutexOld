"""O(1) instrument↔connection mapping + sharding for scaling the live feed.

The universe is the admitted indices + their F&O stocks (and, when subscribed, the
futures/options legs) — each carrying its OWN ``(exchange, segment,
exchange_token)`` security identity. To scale past one websocket's subscription
cap we shard the instruments across N connections; this module owns the PRECISE,
**O(1)** mapping:

* **identity** — ``key = "exchange:segment:exchange_token"`` (hashable; O(1) dedup
  via a set — no instrument is ever subscribed twice);
* **connection assignment** — ``by_key[key] -> connection index`` (O(1) lookup);
  instruments are packed into ``ceil(N / cap)`` shards of at most ``cap`` each;
* **underlying ↔ legs** — ``by_underlying[underlying] -> (key, ...)`` inverted
  index (O(1) lookup of every derivative of an index/stock); routing an incoming
  tick to its instrument is O(1) because the feed snapshot self-identifies.

Honest envelope (Golden Rule #10): every LOOKUP / dedup / route / assign is
**O(1)** (dict/set). BUILDING the plan is **O(N)** one pass and the maps are
**O(N)** space — you must touch each of the N distinct instruments once and store
its assignment. That is NOT an O(1)-total claim (impossible for N distinct
instruments); it is O(1) *amortised per operation* on an O(N) index built once.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Final


@dataclass(frozen=True, slots=True)
class Instrument:
    """One feed-subscription identity (+ its underlying, for the inverted index)."""

    exchange: str
    segment: str
    exchange_token: str
    underlying: str | None = None

    @property
    def key(self) -> str:
        """Stable, hashable composite key — ``exchange:segment:exchange_token``."""
        return f"{self.exchange}:{self.segment}:{self.exchange_token}"

    def feed_dict(self) -> dict[str, str]:
        """The ``{exchange, segment, exchange_token}`` dict the SDK's subscribe expects."""
        return {
            "exchange": self.exchange,
            "segment": self.segment,
            "exchange_token": self.exchange_token,
        }


#: The per-connection subscription cap (operator-confirmed Groww limit; the
#: per-conn-vs-per-account split is UNVERIFIED until the `verify`/`probe` run).
DEFAULT_PER_CONNECTION_CAP: Final[int] = 1000


@dataclass(frozen=True, slots=True)
class ConnectionPlan:
    """A deterministic assignment of instruments to N websocket connections."""

    per_connection_cap: int
    shards: tuple[tuple[Instrument, ...], ...]
    by_key: Mapping[str, int]
    by_underlying: Mapping[str, tuple[str, ...]]

    @property
    def num_connections(self) -> int:
        """How many websocket connections this plan needs."""
        return len(self.shards)

    @property
    def total(self) -> int:
        """Total distinct instruments across all connections."""
        return len(self.by_key)

    def connection_of(self, key: str) -> int:
        """O(1) — which connection index holds *key* (raises ``KeyError`` if absent)."""
        return self.by_key[key]

    def legs_of(self, underlying: str) -> tuple[str, ...]:
        """O(1) — every instrument key derived from *underlying* (``()`` if none)."""
        return self.by_underlying.get(underlying, ())

    def shard(self, index: int) -> tuple[Instrument, ...]:
        """The instruments assigned to connection *index*."""
        return self.shards[index]


def build_plan(
    instruments: Iterable[Instrument], *, per_connection_cap: int = DEFAULT_PER_CONNECTION_CAP
) -> ConnectionPlan:
    """Dedup *instruments* and shard them into ≤*cap*-per-connection bundles.

    One O(N) pass: dedup on the composite key (set membership, O(1) each), pack
    the unique instruments into ``ceil(n / cap)`` contiguous shards, and build the
    O(1) ``by_key`` (instrument → connection) + ``by_underlying`` (underlying →
    legs) indexes. Empty input → zero connections.

    Raises
    ------
    ValueError
        If *per_connection_cap* < 1.
    """
    if per_connection_cap < 1:
        raise ValueError(f"per_connection_cap must be >= 1, got {per_connection_cap}")
    seen: set[str] = set()
    unique: list[Instrument] = []
    for inst in instruments:
        if inst.key not in seen:  # O(1) dedup
            seen.add(inst.key)
            unique.append(inst)
    return _plan_from_unique(unique, per_connection_cap=per_connection_cap)


def _plan_from_unique(
    unique: list[Instrument], *, per_connection_cap: int, boundaries: tuple[int, ...] = ()
) -> ConnectionPlan:
    """Shard pre-deduped *unique* into ≤*cap* bundles + build the O(1) indexes.

    *boundaries* are extra split points (instrument-list offsets) at which a shard
    MUST break even if the cap is not yet reached — used by
    :func:`build_grouped_plan` to keep the spot group and the leg group in
    separate shards. ``()`` (the default) reproduces :func:`build_plan`'s contiguous
    cap-only packing exactly. The work is O(N): one pass to cut shards, one pass to
    fill ``by_key``, one pass to fill ``by_underlying``; the maps are O(N) space.
    """
    cuts = sorted({0, len(unique), *boundaries})
    shards: list[tuple[Instrument, ...]] = []
    for lo, hi in pairwise(cuts):
        for start in range(lo, hi, per_connection_cap):
            shards.append(tuple(unique[start : min(start + per_connection_cap, hi)]))
    shard_tuple = tuple(shards)
    by_key: dict[str, int] = {}
    for connection_index, shard in enumerate(shard_tuple):
        for inst in shard:
            by_key[inst.key] = connection_index
    grouped: dict[str, list[str]] = {}
    for inst in unique:
        if inst.underlying:
            grouped.setdefault(inst.underlying, []).append(inst.key)
    by_underlying = {underlying: tuple(keys) for underlying, keys in grouped.items()}
    return ConnectionPlan(
        per_connection_cap=per_connection_cap,
        shards=shard_tuple,
        by_key=by_key,
        by_underlying=by_underlying,
    )


def build_grouped_plan(
    spot: Sequence[Instrument],
    legs: Sequence[Instrument],
    *,
    per_connection_cap: int = DEFAULT_PER_CONNECTION_CAP,
) -> ConnectionPlan:
    """Shard *spot* into the FIRST connection(s), *legs* into the SUBSEQUENT ones.

    The operator's "connection #1 = all spot, connections #2…N = legs" model: the
    deduped spot instruments fill shards ``0 .. k`` (``k = ceil(len(spot_unique) /
    cap) - 1``); the deduped legs fill the shards AFTER them — **a shard boundary
    always falls between the spot group and the leg group, so spot and legs are
    NEVER mixed in one shard**. Dedup is the same O(1) set-membership pass as
    :func:`build_plan`, applied spot-first so an instrument present in BOTH groups
    is kept ONCE on its spot connection (spot wins — it rides connection #1).

    Honest envelope (Golden Rule #10): every LOOKUP / dedup / route / assign on the
    returned plan (``connection_of`` / ``legs_of`` / ``shard``) is **O(1)** (dict /
    set / tuple-index). BUILDING the plan is **O(N)** one pass and the maps are
    **O(N)** space — you must touch each of the N distinct instruments once and
    store its assignment. That is NOT an O(1)-total claim (impossible for N distinct
    instruments); it is O(1) *amortised per operation* on an O(N) index built once.

    Empty *spot* → legs start on connection #0; empty *legs* → spot-only (mirrors
    :func:`build_plan`); both empty → zero connections.

    Raises
    ------
    ValueError
        If *per_connection_cap* < 1.
    """
    if per_connection_cap < 1:
        raise ValueError(f"per_connection_cap must be >= 1, got {per_connection_cap}")
    seen: set[str] = set()
    unique: list[Instrument] = []
    for inst in (*spot, *legs):  # spot first → spot wins on a cross-group duplicate
        if inst.key not in seen:  # O(1) dedup
            seen.add(inst.key)
            unique.append(inst)
    # The boundary offset is the count of DISTINCT spot keys (spot fills shards
    # 0..k, legs the shards after) — one O(N) pass, no nested scans.
    boundary = len({inst.key for inst in spot})
    return _plan_from_unique(unique, per_connection_cap=per_connection_cap, boundaries=(boundary,))


def connection_count(
    num_instruments: int, *, per_connection_cap: int = DEFAULT_PER_CONNECTION_CAP
) -> int:
    """How many connections *num_instruments* needs at *cap* per connection (O(1))."""
    if per_connection_cap < 1:
        raise ValueError(f"per_connection_cap must be >= 1, got {per_connection_cap}")
    return math.ceil(num_instruments / per_connection_cap) if num_instruments > 0 else 0


__all__ = [
    "DEFAULT_PER_CONNECTION_CAP",
    "ConnectionPlan",
    "Instrument",
    "build_grouped_plan",
    "build_plan",
    "connection_count",
]
