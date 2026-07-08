"""Tests for the dashboard rate-panel data plane (DEC-DASHBOARD-RATE-PANEL-001).

Covers the testable surface: the new measured ``effective_rps`` / ``latency_ms_p95``
panel fields, ``DashboardState.update_throughput``, the ``make_rate_observer``
factory that maps a (``RateController``, ``CallMeter``) snapshot pair onto the
panel, the ``/api/v1/state`` exposure, and the dedicated O(1) ``/api/v1/rate``
route, plus ``_observe_rate``'s no-op guards. The live PUSH that feeds these
during a real Dhan pull is per-pull live-only glue (``# pragma: no cover``,
wired in ``pull_controller`` here); its guard is tested, its wiring is exercised
by the dry-run e2e route tests.
"""

from __future__ import annotations

# Whitebox: the _observe_rate guard test imports the module-private helper.
# pyright: reportPrivateUsage=none
from collections.abc import Iterator

import pytest
from brutex.dashboard.app import DashboardState, RateLimitPanel, make_rate_observer


def test_rate_panel_defaults_include_effective_and_latency() -> None:
    panel = RateLimitPanel()
    assert panel.effective_rps == 0.0
    assert panel.latency_ms_p95 == 0.0


def test_rate_panel_defaults_include_instantaneous_rps_gauge() -> None:
    """DEC-DASHBOARD-RPS1S-PANEL-001: the per-second issue gauge defaults to 0."""
    panel = RateLimitPanel()
    assert panel.rps_1s == 0
    assert panel.rps_1s_max == 0


def test_update_throughput_sets_measured_fields() -> None:
    state = DashboardState()
    state.update_throughput(effective_rps=4.07, latency_ms_p95=174.4)
    assert state.rate_limit.effective_rps == 4.07
    assert state.rate_limit.latency_ms_p95 == 174.4
    # The rps gauge defaults to 0 when the caller omits it (back-compat).
    assert state.rate_limit.rps_1s == 0
    assert state.rate_limit.rps_1s_max == 0


def test_update_throughput_sets_instantaneous_rps_gauge() -> None:
    """DEC-DASHBOARD-RPS1S-PANEL-001: the trailing-1s count + run peak land on the panel."""
    state = DashboardState()
    state.update_throughput(effective_rps=4.9, latency_ms_p95=150.0, rps_1s=5, rps_1s_max=5)
    assert state.rate_limit.rps_1s == 5
    assert state.rate_limit.rps_1s_max == 5


def test_make_rate_observer_maps_both_snapshots_onto_the_panel() -> None:
    state = DashboardState()
    observe = make_rate_observer(state)
    # controller snapshot {current_rps, recent_hits} + meter snapshot
    # {effective_rps, latency, rps_1s, rps_1s_max}
    observe(
        {"current_rps": 3.0, "recent_hits": 7},
        {"effective_rps": 6.08, "latency_ms_p95": 198.3, "rps_1s": 4, "rps_1s_max": 5},
    )
    assert state.rate_limit.current_rps == 3.0
    assert state.rate_limit.recent_429_count == 7
    assert state.rate_limit.effective_rps == 6.08
    assert state.rate_limit.latency_ms_p95 == 198.3
    assert state.rate_limit.rps_1s == 4
    assert state.rate_limit.rps_1s_max == 5
    # A real spot CallMeter snapshot carries no ``meter_present`` key → defaults True
    # (DEC-RATE-WIDGET-FNO-HONEST-001: the meter-fed contract).
    assert state.rate_limit.meter_present is True


def test_rate_panel_default_meter_present_is_true() -> None:
    """DEC-RATE-WIDGET-FNO-HONEST-001: the panel defaults to the meter-fed contract."""
    panel = RateLimitPanel()
    assert panel.meter_present is True


def test_update_throughput_records_meter_present_flag() -> None:
    """DEC-RATE-WIDGET-FNO-HONEST-001: update_throughput stores the meter-present flag."""
    state = DashboardState()
    state.update_throughput(effective_rps=0.0, latency_ms_p95=0.0, meter_present=False)
    assert state.rate_limit.meter_present is False
    # And back to the meter-fed contract on a real spot tick.
    state.update_throughput(effective_rps=4.0, latency_ms_p95=10.0, meter_present=True)
    assert state.rate_limit.meter_present is True


def test_make_rate_observer_maps_meter_absent_sentinel() -> None:
    """DEC-RATE-WIDGET-FNO-HONEST-001: the sampler's meter_present=False reaches the panel.

    The admission-only :class:`RatePanelSampler` pushes a meter snapshot of just
    ``{"meter_present": False}`` — the observer must map it onto the panel + keep the
    (absent) ``effective_rps`` an honest 0, never a fabricated completed-rate.
    """
    state = DashboardState()
    observe = make_rate_observer(state)
    observe(
        {"current_rps": 8.0, "recent_hits": 0, "recent_minute": 210},
        {"meter_present": False},
    )
    assert state.rate_limit.meter_present is False
    assert state.rate_limit.effective_rps == 0.0
    assert state.rate_limit.recent_minute == 210


def test_make_rate_observer_tolerates_missing_keys() -> None:
    state = DashboardState()
    make_rate_observer(state)({}, {})  # empty snapshots → defaults, never a KeyError
    assert state.rate_limit.current_rps == 0.0
    assert state.rate_limit.recent_429_count == 0


def test_make_rate_observer_ignores_snapshot_quota_keys() -> None:
    """A controller snapshot carrying ``quota_*`` keys maps cleanly, gauge-free.

    DEC-QUOTA-VERDICT-MOOT-001: the panel's daily-quota gauge fields are DELETED
    (Groww has no daily quota; no production controller wires a tracker), so a
    snapshot from a hypothetically re-wired tracker must still map without a
    ``TypeError`` — the quota keys are simply not surfaced, and the serialised
    panel carries no ``quota_*`` key.
    """
    state = DashboardState()
    observe = make_rate_observer(state)
    observe(
        {
            "current_rps": 5.0,
            "recent_hits": 0,
            "quota_used": 1234,
            "quota_remaining": 98766,
            "quota_limit": 100000,
        },
        {},
    )
    assert state.rate_limit.current_rps == 5.0
    rl = state.to_dict()["rate_limit"]
    assert not any(key.startswith("quota_") for key in rl)


def test_observe_rate_skips_without_observer_or_controller() -> None:
    """The live push helper is a no-op for a default (no observer) / dry-run (no controller) pull."""
    from typing import cast

    from brutex.adapters.protocol import MarketAdapter
    from brutex.adapters.rate_common import RateController
    from brutex.dashboard.pull_controller import _observe_rate

    calls: list[object] = []

    def _obs(rate: object, meter: object) -> None:
        calls.append((rate, meter))

    # The guard returns BEFORE touching the adapter/controller, so these dummy
    # casts are never dereferenced — they only satisfy the static types.
    adapter = cast(MarketAdapter, object())
    controller = cast(RateController, object())
    _observe_rate(None, controller, adapter)  # observer None → skip
    _observe_rate(_obs, None, adapter)  # controller None (dry-run) → skip
    assert calls == []


def test_state_to_dict_exposes_the_new_rate_fields() -> None:
    state = DashboardState()
    state.update_rate_limit(current_rps=5.0, available_tokens=2.0, recent_429_count=1)
    state.update_throughput(effective_rps=4.9, latency_ms_p95=150.0, rps_1s=4, rps_1s_max=5)
    rl = state.to_dict()["rate_limit"]
    assert rl["current_rps"] == 5.0
    assert rl["effective_rps"] == 4.9
    assert rl["latency_ms_p95"] == 150.0
    assert rl["recent_429_count"] == 1
    # DEC-DASHBOARD-RPS1S-PANEL-001: the instantaneous gauge is serialised too.
    assert rl["rps_1s"] == 4
    assert rl["rps_1s_max"] == 5


class TestRateRoute:
    @pytest.fixture
    def client(self) -> Iterator[object]:
        pytest.importorskip("fastapi")
        from brutex.dashboard.app import build_app
        from fastapi.testclient import TestClient

        state = DashboardState()
        state.update_rate_limit(current_rps=5.0, available_tokens=3.0, recent_429_count=2)
        state.update_throughput(effective_rps=4.8, latency_ms_p95=160.0, rps_1s=5, rps_1s_max=5)
        with TestClient(build_app(state)) as c:
            yield c

    def test_rate_route_returns_o1_panel(self, client: object) -> None:
        resp = client.get("/api/v1/rate")  # type: ignore[attr-defined]
        assert resp.status_code == 200
        body = resp.json()
        assert body["current_rps"] == 5.0
        assert body["effective_rps"] == 4.8
        assert body["latency_ms_p95"] == 160.0
        assert body["recent_429_count"] == 2
        # DEC-DASHBOARD-RPS1S-PANEL-001: the instantaneous gauge rides the same route.
        assert body["rps_1s"] == 5
        assert body["rps_1s_max"] == 5


# The former ``TestQuotaRoute`` + ``_FakeQuotaTracker`` are DELETED with their
# subject: ``GET /api/v1/quota`` + the ``build_app(quota_tracker=)`` param are
# gone (DEC-QUOTA-VERDICT-MOOT-001 — Groww has no daily quota, nothing ever
# injected a tracker in production). The route-removed / no-quota-keys pins
# live in ``tests/unit/test_quota_verdict_moot.py``.


# ═══════════════════════════════════════════════════════════════════════════
# failed_rps wiring (DEC-PULL-TELEMETRY-TRIAD-001) — the meter's measured
# failed-calls rate flows meter snapshot → observer → panel.
# ═══════════════════════════════════════════════════════════════════════════


def test_rate_panel_default_failed_rps_is_zero() -> None:
    """The panel's failed-calls rate defaults 0.0 — byte-identical pre-decision shape."""
    assert RateLimitPanel().failed_rps == 0.0


def test_update_throughput_records_failed_rps() -> None:
    """``update_throughput`` stores the meter's measured failed_rps (default 0.0)."""
    state = DashboardState()
    state.update_throughput(effective_rps=0.0, latency_ms_p95=0.0, failed_rps=2.4)
    assert state.rate_limit.failed_rps == 2.4
    # Omitting it (an older caller) resets to the honest 0.0 default — never sticky.
    state.update_throughput(effective_rps=1.0, latency_ms_p95=5.0)
    assert state.rate_limit.failed_rps == 0.0


def test_make_rate_observer_maps_failed_rps_and_defaults_absent_to_zero() -> None:
    """The observer reads the meter snapshot's ``failed_rps``; an older snapshot (or the
    admission-only sampler sentinel) without the key maps to an honest 0.0."""
    state = DashboardState()
    observe = make_rate_observer(state)
    observe({}, {"effective_rps": 0.0, "failed_rps": 3.1})
    assert state.rate_limit.failed_rps == 3.1
    observe({}, {"effective_rps": 0.0})  # key absent → 0.0, never a stale carry-over
    assert state.rate_limit.failed_rps == 0.0
    observe({}, {"meter_present": False})  # the sampler sentinel carries no failed_rps
    assert state.rate_limit.failed_rps == 0.0
    assert state.rate_limit.meter_present is False
