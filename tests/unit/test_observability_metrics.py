"""Unit tests for brutex.observability.metrics."""

from __future__ import annotations

import math

import pytest

from brutex.observability.metrics import (
    DEFAULT_HISTOGRAM_BUCKETS_SECONDS,
    MetricKind,
    MetricRegistry,
    MetricsError,
    get_registry,
    render_prometheus,
    reset_global_registry,
)


# ────────────────────────────────────────────────────────────────────────
# Registration
# ────────────────────────────────────────────────────────────────────────


class TestRegistration:
    def test_counter_registers_once(self) -> None:
        r = MetricRegistry()
        r.counter("brutex_combos_total", "Total combos processed")
        # Re-registration with same args is idempotent
        r.counter("brutex_combos_total", "Total combos processed")
        assert r.counter_value("brutex_combos_total") == 0.0

    def test_gauge_registers_once(self) -> None:
        r = MetricRegistry()
        r.gauge("brutex_workers_alive", "Live worker count")
        assert r.gauge_value("brutex_workers_alive") == 0.0

    def test_histogram_registers_once(self) -> None:
        r = MetricRegistry()
        r.histogram("brutex_combo_duration_seconds", "Combo latency")
        assert r.histogram_count("brutex_combo_duration_seconds") == 0

    def test_name_collision_across_kinds_raises(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        with pytest.raises(MetricsError, match="already registered"):
            r.gauge("x_total", "x")

    def test_redefine_with_different_labels_raises(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x", labels=("a",))
        with pytest.raises(MetricsError, match="different"):
            r.counter("x_total", "x", labels=("a", "b"))

    def test_histogram_buckets_must_be_strictly_increasing(self) -> None:
        r = MetricRegistry()
        with pytest.raises(MetricsError, match="strictly increasing"):
            r.histogram("h", "h", buckets=(1.0, 1.0, math.inf))

    def test_histogram_buckets_must_end_with_inf(self) -> None:
        r = MetricRegistry()
        with pytest.raises(MetricsError, match="\\+Inf"):
            r.histogram("h", "h", buckets=(1.0, 2.0))


# ────────────────────────────────────────────────────────────────────────
# Counter
# ────────────────────────────────────────────────────────────────────────


class TestCounter:
    def test_inc_by_one(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        r.inc("x_total")
        assert r.counter_value("x_total") == 1.0

    def test_inc_by_amount(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        r.inc("x_total", amount=5.5)
        assert r.counter_value("x_total") == 5.5

    def test_inc_monotonic_accumulates(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        for _ in range(10):
            r.inc("x_total")
        assert r.counter_value("x_total") == 10.0

    def test_negative_inc_rejected(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        with pytest.raises(MetricsError, match="non-negative"):
            r.inc("x_total", amount=-1.0)

    def test_missing_counter_raises(self) -> None:
        r = MetricRegistry()
        with pytest.raises(MetricsError, match="not registered"):
            r.inc("unknown")

    def test_labels(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x", labels=("status",))
        r.inc("x_total", labels=("ok",))
        r.inc("x_total", labels=("ok",))
        r.inc("x_total", labels=("error",))
        assert r.counter_value("x_total", labels=("ok",)) == 2.0
        assert r.counter_value("x_total", labels=("error",)) == 1.0

    def test_label_cardinality_mismatch_raises(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x", labels=("status",))
        with pytest.raises(MetricsError, match="label"):
            r.inc("x_total", labels=("a", "b"))


# ────────────────────────────────────────────────────────────────────────
# Gauge
# ────────────────────────────────────────────────────────────────────────


class TestGauge:
    def test_set_positive(self) -> None:
        r = MetricRegistry()
        r.gauge("g", "g")
        r.set("g", 42.0)
        assert r.gauge_value("g") == 42.0

    def test_set_negative(self) -> None:
        r = MetricRegistry()
        r.gauge("g", "g")
        r.set("g", -10.5)
        assert r.gauge_value("g") == -10.5

    def test_set_overwrites(self) -> None:
        r = MetricRegistry()
        r.gauge("g", "g")
        r.set("g", 10)
        r.set("g", 20)
        assert r.gauge_value("g") == 20.0


# ────────────────────────────────────────────────────────────────────────
# Histogram
# ────────────────────────────────────────────────────────────────────────


class TestHistogram:
    def test_observe_updates_count_and_sum(self) -> None:
        r = MetricRegistry()
        r.histogram("h", "h")
        r.observe("h", 0.5)
        r.observe("h", 1.0)
        assert r.histogram_count("h") == 2
        assert r.histogram_sum("h") == pytest.approx(1.5)

    def test_observe_bucket_assignment(self) -> None:
        r = MetricRegistry()
        r.histogram("h", "h", buckets=(0.1, 1.0, 10.0, math.inf))
        r.observe("h", 0.05)  # -> bucket 0 + 1 + 2 + 3
        r.observe("h", 2.0)  # -> buckets 2, 3
        r.observe("h", 100.0)  # -> bucket 3 only
        # Render and check the +Inf bucket is the total
        rendered = render_prometheus(r)
        assert 'h_bucket{le="+Inf"} 3' in rendered
        assert 'h_bucket{le="0.1"} 1' in rendered


# ────────────────────────────────────────────────────────────────────────
# Prometheus exporter
# ────────────────────────────────────────────────────────────────────────


class TestPrometheusExporter:
    def test_counter_with_no_labels(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x help")
        r.inc("x_total", amount=3)
        out = render_prometheus(r)
        assert "# HELP x_total x help" in out
        assert "# TYPE x_total counter" in out
        assert "x_total 3" in out

    def test_counter_with_labels(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x", labels=("status",))
        r.inc("x_total", labels=("ok",))
        out = render_prometheus(r)
        assert 'x_total{status="ok"} 1' in out

    def test_gauge_exporter(self) -> None:
        r = MetricRegistry()
        r.gauge("g", "g help")
        r.set("g", 42.0)
        out = render_prometheus(r)
        assert "# TYPE g gauge" in out
        assert "g 42" in out

    def test_empty_counter_renders_zero(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x")
        out = render_prometheus(r)
        assert "x_total 0" in out

    def test_label_value_escaping(self) -> None:
        r = MetricRegistry()
        r.counter("x_total", "x", labels=("msg",))
        r.inc("x_total", labels=('hello "world"',))
        out = render_prometheus(r)
        # Expect backslash-escaped quotes
        assert 'x_total{msg="hello \\"world\\""} 1' in out


# ────────────────────────────────────────────────────────────────────────
# Global registry
# ────────────────────────────────────────────────────────────────────────


class TestGlobalRegistry:
    def test_get_registry_returns_singleton(self) -> None:
        reset_global_registry()
        r1 = get_registry()
        r2 = get_registry()
        assert r1 is r2

    def test_reset_drops_state(self) -> None:
        reset_global_registry()
        r1 = get_registry()
        r1.counter("x_total", "x")
        reset_global_registry()
        r2 = get_registry()
        assert r1 is not r2
        with pytest.raises(MetricsError, match="not registered"):
            r2.inc("x_total")
