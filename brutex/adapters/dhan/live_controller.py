"""Dhan live rate controller — the SessionRateController factory seam.

DEC-DHAN-SPOT-PULL-001 (ADR-0022). The Dhan Data API is capped at 5 req/s +
100,000 req/day with NO per-minute governor (PARTIAL: support-page snippets). This
module builds the adaptive :class:`~brutex.adapters.rate_common.RateController`
(ceiling 5 r/s, NO per-minute cap) paired with the 100k/day
:class:`~brutex.orchestrator.quota_tracker.DailyQuotaTracker`, reusing the
vendor-neutral ``rate_common`` machinery verbatim (the Groww ``_build_live_controller``
precedent, CLAUDE.md §11 COMMON).

The zero-arg :func:`build_dhan_live_controller` is the factory the dashboard route
wires into a ``SessionRateController(factory=...)`` so the learned rate carries over
across pulls; it builds the real LMDB-backed quota tracker (``# pragma: no cover`` —
needs disk). The injectable :func:`build_dhan_live_controller_with` is the tested
core (a fake / ``None`` tracker drives every branch with no disk).
"""

from __future__ import annotations

from brutex.adapters.dhan.constants import (
    DHAN_DAILY_QUOTA,
    DHAN_RATE_LIMIT_PER_MIN,
    DHAN_RATE_LIMIT_RPS,
)
from brutex.adapters.rate_common import RateController
from brutex.orchestrator.quota_tracker import DailyQuotaTracker


def build_dhan_daily_quota_tracker() -> DailyQuotaTracker:  # pragma: no cover — real LMDB / disk
    """Build the 100k/day Dhan quota tracker (real LMDB-backed store).

    Honours the 100,000 req/day Data-API cap. A full 3-index June-2022→today
    1-minute pull is ≈ 51 requests, so the cap is never the binding gate in practice
    — but it is honestly enforced (Golden Rule #1: a real cap, not a phantom).
    """
    return DailyQuotaTracker(daily_limit=DHAN_DAILY_QUOTA)


def build_dhan_live_controller_with(*, quota_tracker: DailyQuotaTracker | None) -> RateController:
    """Build the Dhan adaptive controller around an injected *quota_tracker* (the tested seam).

    Ceiling 5 r/s (:data:`~brutex.adapters.dhan.constants.DHAN_RATE_LIMIT_RPS`), NO
    per-minute governor (:data:`~brutex.adapters.dhan.constants.DHAN_RATE_LIMIT_PER_MIN`
    is ``None`` — honestly none, Golden Rule #1). The AIMD ladder still steps DOWN on
    a throttle and recovers up to the ceiling. *quota_tracker* ``None`` is a
    bucket-only mode (tests); production wires the 100k/day tracker.
    """
    return RateController(
        ceiling_rps=DHAN_RATE_LIMIT_RPS,
        ceiling_per_min=DHAN_RATE_LIMIT_PER_MIN,
        quota_tracker=quota_tracker,
    )


def build_dhan_live_controller() -> (
    RateController
):  # pragma: no cover — wires the real LMDB quota tracker
    """The zero-arg session factory: the adaptive controller + the real 100k/day tracker.

    The factory a ``SessionRateController(factory=build_dhan_live_controller)`` wires
    so the learned rate carries over across pulls (the Groww session-persistence
    precedent). ``# pragma: no cover`` — it builds the real LMDB-backed quota tracker;
    :func:`build_dhan_live_controller_with` is the fake-driven tested core.
    """
    return build_dhan_live_controller_with(quota_tracker=build_dhan_daily_quota_tracker())


__all__ = [
    "build_dhan_daily_quota_tracker",
    "build_dhan_live_controller",
    "build_dhan_live_controller_with",
]
