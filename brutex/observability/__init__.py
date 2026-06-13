"""BruteX observability — metrics, logging, tracing.

v1 ships:
* :mod:`brutex.observability.metrics` — in-process metric registry
  with a Prometheus text-format exporter (no ``prometheus_client`` dep)
* :mod:`brutex.observability.logging` — structlog configuration entry
  point (:func:`configure_logging`). First production callers are the
  orchestrator worker + CLI.

Per ``REQUIREMENTS.md §15.1 Metrics (Prometheus)`` and ``NFR-OBS-004``.
"""

from __future__ import annotations

from brutex.observability.logging import configure_logging, reset_configuration
from brutex.observability.metrics import (
    MetricRegistry,
    MetricsError,
    get_registry,
    render_prometheus,
    reset_global_registry,
)

__all__ = [
    "MetricRegistry",
    "MetricsError",
    "configure_logging",
    "get_registry",
    "render_prometheus",
    "reset_configuration",
    "reset_global_registry",
]
