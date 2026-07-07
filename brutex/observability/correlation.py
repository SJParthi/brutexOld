"""pull_id correlation — one id per ingest pull, threaded via structlog contextvars.

The COMMON (CLAUDE.md §11) correlation primitive for the vendor ingest path
(DEC-DHAN-INGEST-LOGGING-001). Every operator-facing pull
(:func:`~brutex.dashboard.pull_controller.pull_candles` /
:func:`~brutex.dashboard.pull_controller.pull_all_spot`) opens a
:func:`pull_id_scope`, which BINDS a single ``pull_id`` into the structlog
context-vars for the whole pull. Two things then carry that id for free:

1. **The already-lit layers** — the transport (the per-call /
   throughput events; DEC-DHAN-CALL-LATENCY-LOGGING-001 #419) and the rate
   controller (``rate.429`` / ``rate.stepdown`` / ``rate.stepup``;
   DEC-DHAN-RATE-LOGGING-001 #420) — inherit ``pull_id`` because
   ``structlog.contextvars.merge_contextvars`` sits first in the configured
   processor chain (``brutex.observability.logging``), so a contextvar bound
   here is merged into EVERY downstream event with no signature plumbing.
2. **The newly-lit ingest spine** — the window / parse / lake-write / series /
   underlying / quarantine events read the bound id back via
   :func:`current_pull_id` and stamp it as an EXPLICIT field, so each event is
   assertable through :func:`structlog.testing.capture_logs` (which deliberately
   strips the contextvars processor, so a contextvar-only id would be invisible
   to the test oracle).

The scope ALWAYS unbinds on exit (``structlog.contextvars.bound_contextvars``
restores the prior value), so one pull's id can never leak into a later pull or
an unrelated request handler sharing the thread.

This module is observability-only: it adds no behaviour to the ingest path —
it only makes the existing flow traceable end-to-end by one grep key.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Generator

import structlog

#: The contextvar / event key every ingest event is correlated by.
PULL_ID_KEY: str = "pull_id"

#: Hex characters kept from a uuid4 — short enough to read in a log line, wide
#: enough (48 bits) that two concurrent pulls never collide in practice.
_PULL_ID_WIDTH: int = 12


def _mint_id(width: int) -> str:
    """Mint a fresh, collision-free hex correlation id of *width* chars.

    Derived from :func:`uuid.uuid4` so it is collision-free across processes and
    machines without any shared state — O(1), no I/O. Shared by both the pull
    and request id minters so the generation logic lives in exactly one place
    (CLAUDE.md §11 COMMON).
    """
    return uuid.uuid4().hex[:width]


def new_pull_id() -> str:
    """Return a fresh, unique pull correlation id (12 hex chars).

    Derived from :func:`uuid.uuid4` so it is collision-free across processes
    and machines without any shared state — O(1), no I/O.
    """
    return _mint_id(_PULL_ID_WIDTH)


def current_pull_id() -> str:
    """Return the ``pull_id`` bound to the current context, or ``""`` if none.

    Reads the structlog context-vars (the id bound by the enclosing
    :func:`pull_id_scope`). Returns the empty string when called outside any
    scope, so a spine function logs a benign ``pull_id=""`` rather than raising
    when exercised in isolation (e.g. a direct unit test that does not open a
    scope). O(bound contextvars) — ``structlog.contextvars.get_contextvars``
    copies the thread's contextvars context and scans EVERY bound var into a
    fresh dict per call (≤ ~3 keys on this repo's threads), no I/O — NOT a
    literal O(1) (Golden Rule #10; matches :func:`emit_progress_beat`'s stated
    cost, whose per-beat channel resolution this function is).
    """
    return str(structlog.contextvars.get_contextvars().get(PULL_ID_KEY, ""))


@contextlib.contextmanager
def pull_id_scope(pull_id: str | None = None) -> Generator[str, None, None]:
    """Bind a ``pull_id`` for the duration of one pull; yield it; unbind on exit.

    Parameters
    ----------
    pull_id
        An explicit correlation id to bind. ``None`` (the default) mints a
        fresh one via :func:`new_pull_id`. Callers pass an explicit id only to
        correlate a pull with an external job id (or to make a test
        deterministic); production pulls let it default.

    Yields
    ------
    str
        The resolved ``pull_id`` actually bound — so the caller can stamp it on
        its own start / done summary events.

    Notes
    -----
    Delegates to :func:`structlog.contextvars.bound_contextvars`, which binds on
    entry and RESTORES the prior value on exit (nesting-safe), guaranteeing the
    id never leaks past the pull. Observability-only — binds a log field, changes
    no ingest behaviour.
    """
    resolved = pull_id if pull_id is not None else new_pull_id()
    with structlog.contextvars.bound_contextvars(**{PULL_ID_KEY: resolved}):
        yield resolved


@contextlib.contextmanager
def inherited_pull_id_scope(pull_id: str | None = None) -> Generator[str, None, None]:
    """Bind a ``pull_id`` that INHERITS an already-bound ambient id; unbind on exit.

    The inherit-aware sibling of :func:`pull_id_scope` (whose ``None`` path always
    MINTS — the COMMON primitive is deliberately untouched). Resolution order:

    * an explicit *pull_id* wins — including an explicit ``""`` (which binds the
      empty string, mirroring ``pull_id_scope("")``; the documented
      binds-empty trap — pass ``None``, not ``""``, to mean "no explicit id");
    * ``None`` with a non-empty ambient id → INHERIT it (a nesting-safe re-bind,
      so the enclosing job-level RUN scope reaches this entrypoint's inner scope
      instead of being shadow-minted over — DEC-PULL-BEAT-PERRUN-001: the spot
      fan-out workers' capture-then-rebind then carries the RUN id, and their
      seam beats route to the run's registered channel);
    * ``None`` with no ambient id → MINT a fresh one (byte-identical to
      :func:`pull_id_scope` for a standalone caller).

    Yields the resolved id; delegates the bind/restore to :func:`pull_id_scope`.
    """
    resolved = pull_id if pull_id is not None else (current_pull_id() or new_pull_id())
    with pull_id_scope(resolved) as pid:
        yield pid


#: The contextvar / event key every dashboard HTTP request is correlated by
#: (DEC-DASHBOARD-REQUEST-LOGGING-001). Distinct from :data:`PULL_ID_KEY`: a
#: request id tags one ``/api/...`` call, a pull id tags one ingest pull — both
#: ride the same ``merge_contextvars`` processor, so a request that triggers a
#: pull can carry both keys at once.
REQUEST_ID_KEY: str = "request_id"

#: Hex width for a request id — the same 48-bit budget as the pull id.
_REQUEST_ID_WIDTH: int = 12


def new_request_id() -> str:
    """Return a fresh, unique dashboard-request correlation id (12 hex chars) — O(1)."""
    return _mint_id(_REQUEST_ID_WIDTH)


def current_request_id() -> str:
    """Return the ``request_id`` bound to the current context, or ``""`` if none — O(1)."""
    return str(structlog.contextvars.get_contextvars().get(REQUEST_ID_KEY, ""))


@contextlib.contextmanager
def request_id_scope(request_id: str | None = None) -> Generator[str, None, None]:
    """Bind a ``request_id`` for one dashboard request; yield it; unbind on exit.

    Mirrors :func:`pull_id_scope` for the dashboard's per-request logging
    middleware (DEC-DASHBOARD-REQUEST-LOGGING-001). An inbound ``X-Request-ID``
    header is passed as *request_id* (so a client can correlate one call across
    services / its own logs); ``None`` (the default) mints a fresh id via
    :func:`new_request_id`.

    Yields
    ------
    str
        The resolved ``request_id`` actually bound — so the middleware can echo
        it back in the ``X-Request-ID`` response header.

    Notes
    -----
    Delegates to :func:`structlog.contextvars.bound_contextvars`, which binds on
    entry and RESTORES the prior value on exit, so the id never leaks to the next
    request handled on the same worker thread. Observability-only.
    """
    resolved = request_id if request_id is not None else new_request_id()
    with structlog.contextvars.bound_contextvars(**{REQUEST_ID_KEY: resolved}):
        yield resolved


#: The contextvar / event key every brute-force SWEEP RUN is correlated by
#: (DEC-SWEEP-UNKILLABLE-CORE-001 R43). Distinct from :data:`PULL_ID_KEY` /
#: :data:`REQUEST_ID_KEY`: a run id tags one sweep run — all three ride the
#: same ``merge_contextvars`` processor, so a dashboard request that launches a
#: sweep can carry ``request_id`` AND ``run_id`` at once.
RUN_ID_KEY: str = "run_id"


def current_run_id() -> str:
    """Return the ``run_id`` bound to the current context, or ``""`` if none.

    The run twin of :func:`current_pull_id` — same contextvar read, same
    outside-any-scope empty-string contract, same O(bound contextvars) cost
    envelope (Golden Rule #10 — never claimed a literal O(1)).
    """
    return str(structlog.contextvars.get_contextvars().get(RUN_ID_KEY, ""))


@contextlib.contextmanager
def run_id_scope(run_id: str) -> Generator[str, None, None]:
    """Bind a ``run_id`` for the duration of one sweep run; yield it; unbind on exit.

    The sweep twin of :func:`pull_id_scope` with ONE deliberate divergence:
    there is **no mint-on-``None`` path**. A sweep's ``run_id`` IS the
    deterministic grid identity (``grid_identity(grid)`` — BLAKE3 over the
    axes + repro hashes, design D-1 / R43); minting a random id here would
    fabricate an identity that matches no artifact on disk (Golden Rule #1),
    so the caller must always pass the real one. Observability-only: it binds
    a log field via ``structlog.contextvars.bound_contextvars`` (bind on
    entry, RESTORE the prior value on exit — nesting-safe, never leaks past
    the run), changing no sweep behaviour.

    Parameters
    ----------
    run_id
        The run's grid-identity digest (the caller's ``grid_identity(grid)``).

    Yields
    ------
    str
        The bound ``run_id`` (verbatim) — so the caller can stamp it on its
        own start / done summary events, mirroring :func:`pull_id_scope`.
    """
    with structlog.contextvars.bound_contextvars(**{RUN_ID_KEY: run_id}):
        yield run_id


__all__ = [
    "PULL_ID_KEY",
    "REQUEST_ID_KEY",
    "RUN_ID_KEY",
    "current_pull_id",
    "current_request_id",
    "current_run_id",
    "inherited_pull_id_scope",
    "new_pull_id",
    "new_request_id",
    "pull_id_scope",
    "request_id_scope",
    "run_id_scope",
]
