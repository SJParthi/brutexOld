"""Test-only SYNTHETIC feed-vendor profile (DEC-VENDOR-PROFILE-REGISTRY-001).

The WS-C isolation suites need a NON-primary profile, but the concrete
second-vendor profile is the feed-vendor track's own deliverable (WS-D, per
ADR-0021 §12.1) and deliberately does NOT ship in this tree. This fake builds
a minimal, fully-CONC-01-clean synthetic feed profile under the ``testfeed``
token: its own trivial rate/quota factories (never the Groww stack), its own
account-key resolver (a constant test token — never the AWS resolver), the
``lambda: False`` no-guard binding (never ``None`` — the CONC-01 type law),
its own ``feeds/<vendor>/`` write domain + feed-root flock lock domain, the
v1 ``clamp_to_prev_session`` today-completeness declaration, and
``no_intent_persist=True`` (the NG-2 v1 ruling).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from brutex.dashboard.vendor_profile import (
    IncrementalityCapability,
    LockDomain,
    PagingProfile,
    TodayCompletenessBinding,
    VendorCapability,
    VendorPullProfile,
)
from brutex.data.write_domain import feeds_write_domain


class FakeFeedRateController:
    """A trivial per-instance stand-in for a feed vendor's rate-controller stack."""


class FakeFeedQuotaTracker:
    """A trivial per-instance stand-in for a feed vendor's daily-quota tracker."""


def _feed_history_floor(today: date) -> date:
    """A synthetic rolling floor (today minus ~5 years, day-clamped)."""
    return date(today.year - 5, today.month, 1)


def synthetic_feed_profile(
    lake_root: Path,
    *,
    vendor: str = "testfeed",
    no_intent_persist: bool = True,
) -> VendorPullProfile:
    """Build the synthetic ``testfeed`` profile over *lake_root* (CONC-01-clean).

    Every profile gets its OWN fresh rate factory + account-key resolver
    (per-call closures — two synthetic profiles must legally coexist in one
    registry, so the shared-identity CONC-01 arms must not fire between
    them). *no_intent_persist* is exposed so the construction-refusal test
    can drive the ``False`` arm (a feed profile demanding the primary intent
    store is a typed refusal — the v1 ISOL-02/NG-2 ruling).
    """
    subtree = f"feeds/{vendor}"

    def _rate_factory() -> FakeFeedRateController:
        return FakeFeedRateController()

    def _account_key() -> str | None:
        return f"{vendor}-account"

    return VendorPullProfile(
        vendor=vendor,
        write_domain=feeds_write_domain(lake_root, subtree),
        capabilities=VendorCapability(
            spot=True,
            futures=False,
            options_shape="rolling_atm",
            metadata=False,
            daily_source="native",
        ),
        daily_source="native",
        history_floor=_feed_history_floor,
        paging=PagingProfile(max_days_per_request=(("spot", 90), ("options", 30))),
        rate_factory=_rate_factory,
        quota_factory=FakeFeedQuotaTracker,
        reauth_binding=None,
        account_key_resolver=_account_key,
        lock_domain=LockDomain(kind="feed_root_flock", feed_root=lake_root / subtree),
        market_hours_guard=lambda: False,  # the no-guard binding IS a callable (CONC-01)
        today_completeness=TodayCompletenessBinding(kind="clamp_to_prev_session"),
        no_intent_persist=no_intent_persist,
        incrementality=IncrementalityCapability(kind="lake_tail_scan"),
        greeks_seam=None,
        ledger_markets=(),
    )
