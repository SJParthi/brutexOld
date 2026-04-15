"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the broker's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

v1 ships exactly one adapter: :mod:`brutex.adapters.groww`.
"""

from __future__ import annotations

__all__: list[str] = []
