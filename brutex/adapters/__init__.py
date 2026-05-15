"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the broker's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

The contract is defined in :mod:`brutex.adapters.protocol`. The Dhan
adapter (Track 1) will land in Phase 2 of the Track 1 build plan
(see ``docs/TRACK1_PHASE_PLAN.md``). No adapter ships in this
intermediate state — Track 1 is at Phase 0 (docs only).
"""

from __future__ import annotations

__all__: list[str] = []
