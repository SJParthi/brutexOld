"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the upstream vendor's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

The contract is defined in :mod:`brutex.adapters.protocol`. BruteX is
**Dhan-only** (DEC-DHAN-FINALIZE-001, 2026-06-01): the live adapter is
:mod:`brutex.adapters.dhan` — ``auth`` (manual access-token + client-id),
``client`` (plain REST over the injected urllib transport), ``body`` /
``parser`` (request/response shapes), ``run_ingest`` / ``option_ingest`` /
``option_universe_ingest`` (the spot + rolling-option pulls). The earlier
Groww adapter (DEC-VENDOR-PIVOT-001) was removed in full; pre-removal state
at git tags `v-pre-groww-pivot` (the original wipe) and the #267 merge.
"""

from __future__ import annotations

__all__: list[str] = []
