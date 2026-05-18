"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the upstream vendor's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

The contract is defined in :mod:`brutex.adapters.protocol`. The Dhan
adapter (Track 1) is built incrementally across Phase 2:

* PR #51 (DEC-P0-033) — :mod:`brutex.adapters.dhan.constants`
  (DhanSpotInstrument singletons for the 4 Track 1 instruments).
* PR #2a (DEC-P0-034) — :mod:`.timeconv`, :mod:`.body`, :mod:`.parser`
  (pure-function foundation).
* PR #2b (DEC-P0-035, planned) — :class:`brutex.adapters.dhan.client.DhanClient`
  (HTTP transport + classify_response state machine).
* PR #2c (DEC-P0-036, planned) — trust_env=False + SSRF guard + real-Dhan smoke.
"""

from __future__ import annotations

__all__: list[str] = []
