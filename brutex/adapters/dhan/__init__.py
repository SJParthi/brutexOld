"""Dhan v2 historical-data adapter — Track 1 single data source.

Modules in this package (incremental Phase 2 buildup):

* :mod:`.constants` — ``DhanSpotInstrument`` singletons for the 4 Track 1
  instruments (NIFTY, BANKNIFTY, SENSEX, INDIAVIX). PR #51 / DEC-P0-033.
* :mod:`.timeconv` — IST↔UTC conversion (Dhan returns IST-anchored
  epoch seconds; downstream consumers want tz-aware UTC). PR #2a / DEC-P0-034.
* :mod:`.body` — Request body builder for ``/v2/charts/intraday`` and
  ``/v2/charts/historical`` with field-name and interval-type
  invariants (interval is a string for intraday, OMITTED for historical;
  oi=False locked for Track 1 INDEX instruments). PR #2a.
* :mod:`.parser` — 6-parallel-array JSON → ``tuple[OHLCBar, ...]``
  with length-mismatch → ``DhanErrorDH908`` synthesis and
  deterministic sort + dedup. PR #2a.
* :mod:`.options_constants` — Track 2 options-trading metadata
  (separate from the Track 1 spot constants above).

Planned (lands in subsequent PRs):

* ``.client`` — :class:`DhanClient` async HTTP transport with classify_response
  state machine, integrating :mod:`brutex.orchestrator.rate_limiter` +
  :mod:`brutex.orchestrator.quota_tracker` + :mod:`brutex.orchestrator.dhan_errors`.
  Lands in PR #2b / DEC-P0-035.

Operator auth path: the token loader (PR #2b) reads from a file or env
var per the ADR landing alongside ``.client`` — see ``docs/adr/`` for the
final decision; until then the adapter modules above are token-free.
"""
