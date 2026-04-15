"""In-process metric registry with a Prometheus text-format exporter.

Implements ``REQUIREMENTS.md §15.1 Metrics (Prometheus)`` and
``NFR-OBS-004``. Zero external dependency — we deliberately do not
pull in ``prometheus_client`` because v1 only needs a tiny subset
(counter + gauge + histogram) and we want the orchestrator package
to stay lean.

## Metric types

* **Counter** — monotonically increasing value. Reset only when the
  process restarts. Typical: ``brutex_combos_total``.
* **Gauge** — arbitrary point-in-time value (can go up or down).
  Typical: ``brutex_workers_alive``.
* **Histogram** — observations binned into buckets, with cumulative
  counters per upper bound + a running sum + total count. Typical:
  ``brutex_combo_duration_seconds``.

## Thread safety

Every metric acquires its own lock during mutation. Reads into the
exporter take a snapshot under the same lock. Safe to use from
Ray actors, subscribers, the dashboard web server, and the ingest
pipeline concurrently.

## Labels

Label support is intentionally minimal in v1: each metric has a
fixed set of label keys declared at registration time, and values
are passed as a tuple when recording. Example::

    registry.counter(
        "brutex_errors_total",
        "Total errors by type",
        labels=("type",),
    )
    registry.inc("brutex_errors_total", labels=("timeout",))

The exporter renders this as::

    # HELP brutex_errors_total Total errors by type
    # TYPE brutex_errors_total counter
    brutex_errors_total{type="timeout"} 1
"""

from __future__ import annotations

import itertools
import math
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from brutex.models import BruteXError

# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class MetricsError(BruteXError):
    """Base class for metric-registry errors."""


# ────────────────────────────────────────────────────────────────────────
# Types
# ────────────────────────────────────────────────────────────────────────


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


#: Default histogram buckets in seconds — covers per-combo (≤1s),
#: per-chunk (≤30s), and per-phase (≤600s) latencies.
DEFAULT_HISTOGRAM_BUCKETS_SECONDS: Final[tuple[float, ...]] = (
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    300.0,
    600.0,
    math.inf,
)


#: Label tuple key for the "no labels" case.
_EMPTY_LABELS: Final[tuple[str, ...]] = ()


# ────────────────────────────────────────────────────────────────────────
# Metric dataclasses
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _MetricDefinition:
    """Common metadata for every metric type."""

    name: str
    help: str
    kind: MetricKind
    labels: tuple[str, ...]
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class _CounterMetric:
    definition: _MetricDefinition
    values: dict[tuple[str, ...], float] = field(default_factory=dict)


@dataclass(slots=True)
class _GaugeMetric:
    definition: _MetricDefinition
    values: dict[tuple[str, ...], float] = field(default_factory=dict)


@dataclass(slots=True)
class _HistogramMetric:
    definition: _MetricDefinition
    buckets: tuple[float, ...]
    # label_values -> (bucket_index -> cumulative_count)
    bucket_counts: dict[tuple[str, ...], list[int]] = field(default_factory=dict)
    sums: dict[tuple[str, ...], float] = field(default_factory=dict)
    counts: dict[tuple[str, ...], int] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────
# MetricRegistry
# ────────────────────────────────────────────────────────────────────────


class MetricRegistry:
    """Collection of named metrics.

    Register each metric once at startup via :meth:`counter`,
    :meth:`gauge`, or :meth:`histogram`. Record values via
    :meth:`inc`, :meth:`set`, or :meth:`observe`. Render for
    Prometheus scraping via :func:`render_prometheus`.

    Not typically instantiated directly — use :func:`get_registry`
    for the process-global singleton.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _CounterMetric] = {}
        self._gauges: dict[str, _GaugeMetric] = {}
        self._histograms: dict[str, _HistogramMetric] = {}

    # ── Registration ───────────────────────────────────────────────

    def counter(
        self,
        name: str,
        help_text: str,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
    ) -> None:
        """Declare a counter. Safe to call more than once with the same args."""
        with self._lock:
            if name in self._counters:
                self._validate_same_definition(
                    self._counters[name].definition, labels, MetricKind.COUNTER
                )
                return
            self._reject_name_collision(name)
            self._counters[name] = _CounterMetric(
                definition=_MetricDefinition(
                    name=name, help=help_text, kind=MetricKind.COUNTER, labels=labels
                )
            )

    def gauge(
        self,
        name: str,
        help_text: str,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
    ) -> None:
        """Declare a gauge. Idempotent on identical re-registration."""
        with self._lock:
            if name in self._gauges:
                self._validate_same_definition(
                    self._gauges[name].definition, labels, MetricKind.GAUGE
                )
                return
            self._reject_name_collision(name)
            self._gauges[name] = _GaugeMetric(
                definition=_MetricDefinition(
                    name=name, help=help_text, kind=MetricKind.GAUGE, labels=labels
                )
            )

    def histogram(
        self,
        name: str,
        help_text: str,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
        buckets: tuple[float, ...] = DEFAULT_HISTOGRAM_BUCKETS_SECONDS,
    ) -> None:
        """Declare a histogram."""
        if not buckets or buckets[-1] != math.inf:
            raise MetricsError(
                f"Histogram {name!r} buckets must be non-empty and end with +Inf (got {buckets!r})"
            )
        if any(a >= b for a, b in itertools.pairwise(buckets)):
            raise MetricsError(f"Histogram {name!r} buckets must be strictly increasing")
        with self._lock:
            if name in self._histograms:
                self._validate_same_definition(
                    self._histograms[name].definition, labels, MetricKind.HISTOGRAM
                )
                return
            self._reject_name_collision(name)
            self._histograms[name] = _HistogramMetric(
                definition=_MetricDefinition(
                    name=name,
                    help=help_text,
                    kind=MetricKind.HISTOGRAM,
                    labels=labels,
                ),
                buckets=buckets,
            )

    def _reject_name_collision(self, name: str) -> None:
        if name in self._counters or name in self._gauges or name in self._histograms:
            raise MetricsError(f"Metric name {name!r} already registered")

    def _validate_same_definition(
        self,
        existing: _MetricDefinition,
        labels: tuple[str, ...],
        kind: MetricKind,
    ) -> None:
        if existing.kind != kind:
            raise MetricsError(
                f"Metric {existing.name!r} already registered as "
                f"{existing.kind.value}, cannot redefine as {kind.value}"
            )
        if existing.labels != labels:
            raise MetricsError(
                f"Metric {existing.name!r} re-registered with different "
                f"labels: existing={existing.labels}, new={labels}"
            )

    # ── Recording ──────────────────────────────────────────────────

    def inc(
        self,
        name: str,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
        amount: float = 1.0,
    ) -> None:
        """Increment a counter.

        Raises
        ------
        MetricsError
            If the counter is not registered or *amount* is negative
            (counters are monotonic).
        """
        if amount < 0:
            raise MetricsError(f"Counter increment must be non-negative, got {amount}")
        with self._lock:
            if name not in self._counters:
                raise MetricsError(f"Counter {name!r} is not registered")
            counter = self._counters[name]
            self._validate_label_cardinality(counter.definition, labels)
        with counter.definition.lock:
            counter.values[labels] = counter.values.get(labels, 0.0) + amount

    def set(
        self,
        name: str,
        value: float,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
    ) -> None:
        """Set a gauge to *value*."""
        with self._lock:
            if name not in self._gauges:
                raise MetricsError(f"Gauge {name!r} is not registered")
            gauge = self._gauges[name]
            self._validate_label_cardinality(gauge.definition, labels)
        with gauge.definition.lock:
            gauge.values[labels] = float(value)

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: tuple[str, ...] = _EMPTY_LABELS,
    ) -> None:
        """Record an observation into a histogram."""
        with self._lock:
            if name not in self._histograms:
                raise MetricsError(f"Histogram {name!r} is not registered")
            histogram = self._histograms[name]
            self._validate_label_cardinality(histogram.definition, labels)
        with histogram.definition.lock:
            counts = histogram.bucket_counts.setdefault(labels, [0] * len(histogram.buckets))
            for i, upper in enumerate(histogram.buckets):
                if value <= upper:
                    counts[i] += 1
            histogram.sums[labels] = histogram.sums.get(labels, 0.0) + value
            histogram.counts[labels] = histogram.counts.get(labels, 0) + 1

    def _validate_label_cardinality(
        self,
        definition: _MetricDefinition,
        labels: tuple[str, ...],
    ) -> None:
        if len(labels) != len(definition.labels):
            raise MetricsError(
                f"Metric {definition.name!r} expects {len(definition.labels)} "
                f"label values ({definition.labels}), got {len(labels)}"
            )

    # ── Read-only accessors for tests ──────────────────────────────

    def counter_value(self, name: str, *, labels: tuple[str, ...] = _EMPTY_LABELS) -> float:
        return self._counters[name].values.get(labels, 0.0)

    def gauge_value(self, name: str, *, labels: tuple[str, ...] = _EMPTY_LABELS) -> float:
        return self._gauges[name].values.get(labels, 0.0)

    def histogram_count(self, name: str, *, labels: tuple[str, ...] = _EMPTY_LABELS) -> int:
        return self._histograms[name].counts.get(labels, 0)

    def histogram_sum(self, name: str, *, labels: tuple[str, ...] = _EMPTY_LABELS) -> float:
        return self._histograms[name].sums.get(labels, 0.0)


# ────────────────────────────────────────────────────────────────────────
# Prometheus text-format exporter
# ────────────────────────────────────────────────────────────────────────


def render_prometheus(registry: MetricRegistry) -> str:
    """Render every metric in *registry* as Prometheus text format 0.0.4.

    Output is the body of a ``/metrics`` HTTP endpoint. Stable order
    (counter / gauge / histogram, alphabetical within each kind) so
    the output is deterministic for tests.
    """
    out: list[str] = []

    with registry._lock:
        counters = sorted(registry._counters.items())
        gauges = sorted(registry._gauges.items())
        histograms = sorted(registry._histograms.items())

    for name, counter in counters:
        with counter.definition.lock:
            out.append(f"# HELP {name} {counter.definition.help}")
            out.append(f"# TYPE {name} counter")
            items = sorted(counter.values.items())
            if not items:
                out.append(f"{name} 0")
            for labels, value in items:
                out.append(_format_line(name, counter.definition.labels, labels, value))

    for name, gauge in gauges:
        with gauge.definition.lock:
            out.append(f"# HELP {name} {gauge.definition.help}")
            out.append(f"# TYPE {name} gauge")
            items = sorted(gauge.values.items())
            if not items:
                out.append(f"{name} 0")
            for labels, value in items:
                out.append(_format_line(name, gauge.definition.labels, labels, value))

    for name, histogram in histograms:
        with histogram.definition.lock:
            out.append(f"# HELP {name} {histogram.definition.help}")
            out.append(f"# TYPE {name} histogram")
            label_values = sorted(histogram.bucket_counts.keys())
            for labels in label_values:
                counts = histogram.bucket_counts[labels]
                for upper, count in zip(histogram.buckets, counts, strict=False):
                    bucket_label = "+Inf" if upper == math.inf else f"{upper}"
                    extra_labels = (*histogram.definition.labels, "le")
                    extra_values = (*labels, bucket_label)
                    out.append(_format_line(name + "_bucket", extra_labels, extra_values, count))
                out.append(
                    _format_line(
                        name + "_sum",
                        histogram.definition.labels,
                        labels,
                        histogram.sums.get(labels, 0.0),
                    )
                )
                out.append(
                    _format_line(
                        name + "_count",
                        histogram.definition.labels,
                        labels,
                        histogram.counts.get(labels, 0),
                    )
                )

    return "\n".join(out) + "\n"


def _format_line(
    metric_name: str,
    label_keys: tuple[str, ...],
    label_values: tuple[str, ...],
    value: float | int,
) -> str:
    if not label_keys:
        return f"{metric_name} {_format_value(value)}"
    pairs = ",".join(
        f'{k}="{_escape_label(v)}"' for k, v in zip(label_keys, label_values, strict=False)
    )
    return f"{metric_name}{{{pairs}}} {_format_value(value)}"


def _format_value(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if math.isnan(value):
        return "NaN"
    return f"{value:g}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ────────────────────────────────────────────────────────────────────────
# Global registry singleton
# ────────────────────────────────────────────────────────────────────────


_GLOBAL_REGISTRY: MetricRegistry | None = None
_GLOBAL_LOCK = threading.Lock()


def get_registry() -> MetricRegistry:
    """Return the process-global :class:`MetricRegistry`.

    Lazily constructs on first call. Thread-safe.
    """
    global _GLOBAL_REGISTRY  # noqa: PLW0603
    with _GLOBAL_LOCK:
        if _GLOBAL_REGISTRY is None:
            _GLOBAL_REGISTRY = MetricRegistry()
        return _GLOBAL_REGISTRY


def reset_global_registry() -> None:
    """Drop the global registry — test helper only."""
    global _GLOBAL_REGISTRY  # noqa: PLW0603
    with _GLOBAL_LOCK:
        _GLOBAL_REGISTRY = None


__all__ = [
    "DEFAULT_HISTOGRAM_BUCKETS_SECONDS",
    "MetricKind",
    "MetricRegistry",
    "MetricsError",
    "get_registry",
    "render_prometheus",
    "reset_global_registry",
]
