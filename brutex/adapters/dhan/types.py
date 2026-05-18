"""Shared types for the Dhan adapter.

``FetchResult`` is the never-raises return contract of every
:class:`DhanClient` method — a single dataclass that carries either the
parsed bars (on success) or the classified error (on failure), plus the
:class:`Outcome` the orchestrator FSM consumes.

Keeping this in a sibling module (not in ``client.py``) lets the parser
and the chunk planner consume it without circular-import risk.
"""

from __future__ import annotations

from dataclasses import dataclass

from brutex.adapters.dhan.body import HistoricalDailyRequest, IntradayChunkRequest
from brutex.adapters.dhan.parser import OHLCBar
from brutex.orchestrator.dhan_errors import DhanError
from brutex.orchestrator.state import Outcome


@dataclass(frozen=True, slots=True)
class FetchResult:
    """Outcome of a single Dhan fetch.

    Frozen + slots. Exactly one of ``bars`` (non-empty) and ``error`` is
    populated per outcome class:

    * ``Outcome.OK_WITH_DATA`` — ``bars`` non-empty, ``error is None``.
    * ``Outcome.OK_EMPTY`` — ``bars == ()``, ``error is None``.
    * Any error outcome (``RATE_LIMIT`` / ``TIMEOUT`` / ``GENERIC_API_ERROR``
      / ``NOT_FOUND`` / ``NO_DATA`` / ``BAD_REQUEST`` / ``AUTH_FAILED`` /
      ``AUTHZ_FAILED`` / ``FATAL``) — ``bars == ()``, ``error`` is the
      typed :class:`DhanError` subclass that produced the outcome.

    The caller never sees an exception from ``DhanClient.fetch_*`` — the
    classified error lives in ``error`` so the orchestrator can route
    deterministically.

    Attributes
    ----------
    request
        The request that produced this result. Either an
        :class:`IntradayChunkRequest` or :class:`HistoricalDailyRequest`.
        Tracking the request alongside the outcome simplifies audit log
        construction (one CHUNK_DONE event carries everything).
    outcome
        The FSM outcome for this fetch (per
        :class:`brutex.orchestrator.state.Outcome`).
    bars
        Parsed OHLC bars. Empty tuple if outcome is not
        :class:`Outcome.OK_WITH_DATA`.
    error
        The typed :class:`DhanError` subclass on failure; ``None`` on
        success (OK_WITH_DATA / OK_EMPTY).
    """

    request: IntradayChunkRequest | HistoricalDailyRequest
    outcome: Outcome
    bars: tuple[OHLCBar, ...]
    error: DhanError | None
