"""pull_id correlation — one id per ingest pull, threaded via structlog contextvars.

The COMMON (CLAUDE.md §11) correlation primitive for the Dhan ingest path
(DEC-DHAN-INGEST-LOGGING-001). Every operator-facing pull
(:func:`~brutex.dashboard.pull_controller.pull_candles` /
:func:`~brutex.dashboard.pull_controller.pull_all_spot` /
:func:`~brutex.dashboard.option_pull_controller.pull_options`) opens a
:func:`pull_id_scope`, which BINDS a single ``pull_id`` into the structlog
context-vars for the whole pull. Two things then carry that id for free:

1. **The already-lit layers** — the transport (``dhan.call`` /
   ``dhan.throughput``; DEC-DHAN-CALL-LATENCY-LOGGING-001 #419) and the rate
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


def new_pull_id() -> str:
    """Return a fresh, unique pull correlation id (12 hex chars).

    Derived from :func:`uuid.uuid4` so it is collision-free across processes
    and machines without any shared state — O(1), no I/O.
    """
    return uuid.uuid4().hex[:_PULL_ID_WIDTH]


def current_pull_id() -> str:
    """Return the ``pull_id`` bound to the current context, or ``""`` if none.

    Reads the structlog context-vars (the id bound by the enclosing
    :func:`pull_id_scope`). Returns the empty string when called outside any
    scope, so a spine function logs a benign ``pull_id=""`` rather than raising
    when exercised in isolation (e.g. a direct unit test that does not open a
    scope). O(1).
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


__all__ = [
    "PULL_ID_KEY",
    "current_pull_id",
    "new_pull_id",
    "pull_id_scope",
]
