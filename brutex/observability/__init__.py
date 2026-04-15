"""BruteX observability — metrics, logging, tracing.

v1 ships:
* :mod:`brutex.observability.metrics` — in-process metric registry
  with a Prometheus text-format exporter (no ``prometheus_client`` dep)
* structlog is configured in `brutex.__init__` (wired later)

Per ``REQUIREMENTS.md §15.1 Metrics (Prometheus)`` and ``NFR-OBS-004``.
"""

from __future__ import annotations

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
    "get_registry",
    "render_prometheus",
    "reset_global_registry",
]
