"""Per-request structured logging for the dashboard (DEC-DASHBOARD-REQUEST-LOGGING-001).

The dashboard's READ paths were observability-dark: a ``GET /api/v1/...`` only
produced uvicorn's raw ``"GET ... 200"`` access line — no latency, no request
id, no correlation with a concurrent pull — and the 1 s ``/pull-progress`` +
4 s coverage polls flooded that access stream so the ~52 structured BUSINESS
events (``rate.*`` / ``ingest.*`` / ``dhan.*``) were drowned.

This module supplies the small, pure, 100%-unit-testable pieces the dashboard
middleware uses to fix both:

* :func:`emit_request_log` — one structured ``dashboard.request`` event per
  request carrying ``method`` / ``path`` / ``status_code`` / ``latency_ms`` /
  ``request_id`` / ``client``. The high-frequency POLL paths
  (:data:`HIGH_FREQUENCY_POLL_PATHS`) are logged at **DEBUG** so a quiet INFO
  stream shows real activity, not the poll heartbeat; everything else logs at
  **INFO**.
* :func:`silence_uvicorn_access_log` — drops uvicorn's own ``uvicorn.access``
  ``"GET ... 200"`` firehose to ``WARNING`` so it stops duplicating what the
  structured ``dashboard.request`` event now records (called once at dashboard
  startup).

The thin async middleware that times the request, opens a
:func:`~brutex.observability.correlation.request_id_scope`, and calls
:func:`emit_request_log` lives in ``brutex.dashboard.app`` (the FastAPI glue
layer); the request-correlation primitive lives in
``brutex.observability.correlation`` (COMMON, CLAUDE.md §11) — this module owns
only the dashboard-specific level policy + the firehose silencer.
"""

from __future__ import annotations

import logging

import structlog

#: The structured per-request event logger. ``capture_logs`` intercepts it in
#: tests regardless of level, so :func:`emit_request_log` is fully assertable.
_LOG = structlog.get_logger("brutex.dashboard.request")

#: Paths the dashboard polls on a tight cadence (``/pull-progress`` every ~1 s,
#: the coverage summaries every ~4 s — TRACK1_DASHBOARD_DESIGN v29 §6.0). Their
#: per-request event is demoted to DEBUG so they never flood the INFO stream;
#: the data they carry is already on the O(1) cached routes (#437).
HIGH_FREQUENCY_POLL_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/universe/pull-progress",
        "/api/v1/coverage/spot",
        "/api/v1/coverage/options",
        "/api/v1/coverage/counters",
    }
)


def is_high_frequency_poll(path: str) -> bool:
    """Return ``True`` if *path* is a high-frequency poll (logged at DEBUG) — O(1)."""
    return path in HIGH_FREQUENCY_POLL_PATHS


def silence_uvicorn_access_log(level: int = logging.WARNING) -> None:
    """Quiet uvicorn's ``"GET ... 200"`` access firehose to *level* (default WARNING).

    The structured :func:`emit_request_log` ``dashboard.request`` event is now
    the single per-request source of truth (with latency + request id), so
    uvicorn's raw access line is pure duplication + noise. Called once at
    dashboard startup; idempotent (setting a logger level twice is a no-op).
    Leaves ``uvicorn`` / ``uvicorn.error`` untouched so startup + error lines
    still surface.
    """
    logging.getLogger("uvicorn.access").setLevel(level)


def emit_request_log(
    *,
    method: str,
    path: str,
    status_code: int,
    latency_ms: float,
    request_id: str,
    client: str | None,
) -> None:
    """Emit one ``dashboard.request`` event at the path-appropriate level — O(1).

    A high-frequency poll (:func:`is_high_frequency_poll`) logs at DEBUG;
    everything else at INFO. ``latency_ms`` is rounded to 2 dp for log
    readability (it is observability, never a money/statistical metric, so
    rounding here breaks no ranking — CLAUDE.md §4 money rule is unaffected).

    Parameters
    ----------
    method, path
        The HTTP method + request path.
    status_code
        The response status (or ``500`` when the handler raised).
    latency_ms
        Wall-clock handler latency in milliseconds.
    request_id
        The correlation id bound for this request (echoed in ``X-Request-ID``).
    client
        The peer host, or ``None`` when unavailable (e.g. an internal call).
    """
    fields = {
        "method": method,
        "path": path,
        "status_code": status_code,
        "latency_ms": round(latency_ms, 2),
        "request_id": request_id,
        "client": client,
    }
    if is_high_frequency_poll(path):
        _LOG.debug("dashboard.request", **fields)
    else:
        _LOG.info("dashboard.request", **fields)


__all__ = [
    "HIGH_FREQUENCY_POLL_PATHS",
    "emit_request_log",
    "is_high_frequency_poll",
    "silence_uvicorn_access_log",
]
