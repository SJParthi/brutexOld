"""Broker adapters — L1 in the layer stack.

Each adapter is the ONLY component in BruteX that speaks the upstream vendor's
native protocol. Everything above L1 (L2 data lake, L3 feed, L4 exec,
L5 engine, L6 orchestrator, L7 UI) consumes normalised BruteX models
from :mod:`brutex.models` and is broker-agnostic.

The contract is defined in :mod:`brutex.adapters.protocol`. Post
DEC-VENDOR-PIVOT-001 (2026-05-26), the Groww adapter is built
incrementally across Phase 1+:

* Phase 0a (PR #135) — ADR + spec (no adapter code).
* Phase 0b — `scripts/groww_probe.py` empirical probe.
* Phase 1 — :mod:`brutex.adapters.groww.{auth, constants, types}`.
* Phase 2 — :mod:`.body`, :mod:`.parser`, :mod:`.client` + adaptive rate-limiter.
* Phase 3 — :mod:`.ssrf_guard`, :mod:`.timeconv` + :class:`GrowwMarketAdapter`.

The Dhan adapter was wiped 2026-05-26; pre-wipe state at git tag
`v-pre-groww-pivot`.
"""

from __future__ import annotations

__all__: list[str] = []
