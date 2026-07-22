"""Tests for :mod:`brutex.adapters.dhan.live_controller` — the rate controller seam."""

from __future__ import annotations

from brutex.adapters.dhan.live_controller import build_dhan_live_controller_with
from brutex.adapters.rate_common import CEILING_RPS, RateController


def test_bucket_only_controller_at_5_rps_no_per_min() -> None:
    controller = build_dhan_live_controller_with(quota_tracker=None)
    assert isinstance(controller, RateController)
    # Starts at the Dhan 5 r/s ceiling (which equals the vendor-neutral default here).
    assert controller.current_rps == CEILING_RPS == 5.0


def test_controller_accepts_an_injected_quota_tracker() -> None:
    class _FakeQuota:
        def acquire_or_raise(self) -> None:  # pragma: no cover — not exercised by pace here
            return None

    controller = build_dhan_live_controller_with(quota_tracker=_FakeQuota())  # type: ignore[arg-type]
    assert isinstance(controller, RateController)
