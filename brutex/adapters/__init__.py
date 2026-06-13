"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the upstream vendor's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

The contract is defined in :mod:`brutex.adapters.protocol`. BruteX is
**Groww** (ADR-0009): the live adapter is
:mod:`brutex.adapters.groww` — ``auth`` (Groww TOTP: api key + TOTP secret),
``client`` (thin SDK wrapper over the injected ``growwapi`` transport),
``chunks`` / ``parser`` (request/response shapes), and the spot + option
ingest paths.
"""

from __future__ import annotations

__all__: list[str] = []
