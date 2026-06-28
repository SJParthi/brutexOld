"""Unit tests for :mod:`brutex.observability.pull_eta` — honest wall-clock ETA.

GAP C5 — honest pull-ETA (DEC-ENV-PERSIST-SLA-001). ``estimate_pull_eta`` is a
PURE function: ``windows / max(achieved_rps, epsilon)``. No fake O(1), no
"minutes" magic — it is straight division with a zero-rps guard so a stalled
pull never divides by zero.
"""

from __future__ import annotations

import math

import pytest
from brutex.observability.pull_eta import (
    EPSILON_RPS,
    estimate_pull_eta,
    format_eta,
)


def test_basic_division() -> None:
    """6000 windows at 3 r/s ⇒ 2000 s."""
    eta = estimate_pull_eta(6000, 3.0)
    assert eta.seconds == pytest.approx(2000.0)
    assert eta.total_windows == 6000
    assert eta.achieved_rps == pytest.approx(3.0)


def test_hours_field() -> None:
    """The hours field is seconds / 3600."""
    eta = estimate_pull_eta(67000, 3.0)
    assert eta.seconds == pytest.approx(67000 / 3.0)
    assert eta.hours == pytest.approx(67000 / 3.0 / 3600.0)


def test_zero_rps_uses_epsilon_not_inf() -> None:
    """A zero achieved rate is floored at EPSILON_RPS — finite, never inf."""
    eta = estimate_pull_eta(100, 0.0)
    assert math.isfinite(eta.seconds)
    assert eta.seconds == pytest.approx(100 / EPSILON_RPS)


def test_negative_rps_uses_epsilon() -> None:
    """A negative (nonsensical) rate is also floored at EPSILON_RPS."""
    eta = estimate_pull_eta(100, -5.0)
    assert eta.seconds == pytest.approx(100 / EPSILON_RPS)


def test_zero_windows_is_zero_seconds() -> None:
    """Zero windows ⇒ zero duration regardless of rate."""
    assert estimate_pull_eta(0, 3.0).seconds == 0.0


def test_negative_windows_clamped_to_zero() -> None:
    """A negative window count is clamped to zero (no negative duration)."""
    eta = estimate_pull_eta(-10, 3.0)
    assert eta.total_windows == 0
    assert eta.seconds == 0.0


def test_format_eta_seconds() -> None:
    """A sub-minute duration formats in seconds."""
    assert format_eta(estimate_pull_eta(30, 1.0)) == "30s"


def test_format_eta_minutes() -> None:
    """A sub-hour duration formats as Xm Ys."""
    assert format_eta(estimate_pull_eta(150, 1.0)) == "2m 30s"


def test_format_eta_hours() -> None:
    """An hour-plus duration formats as Xh Ym."""
    # 7200 s + 90 s = 2h 1m 30s -> "2h 1m"
    assert format_eta(estimate_pull_eta(7290, 1.0)) == "2h 1m"


def test_pulleta_is_frozen() -> None:
    """:class:`PullEta` is immutable."""
    eta = estimate_pull_eta(10, 1.0)
    with pytest.raises((AttributeError, TypeError)):
        eta.seconds = 1.0  # type: ignore[misc]


def test_worked_example_67k_at_3rps() -> None:
    """The doc headline: ~67k spot windows at a MEASURED ~3 r/s ⇒ ~6.2 hours."""
    eta = estimate_pull_eta(67000, 3.0)
    assert 6.0 < eta.hours < 6.3
