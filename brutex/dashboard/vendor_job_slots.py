"""Per-vendor ``BackgroundPullJob`` SLOTS keyed by vendor token (WS-C scaffold).

DEC-VENDOR-PROFILE-REGISTRY-001 (WS-C of the dual-vendor orchestration track),
implementing the ADR-0021 Decision 5 job-slot row per the frozen design
contract `DEC-DUAL-VENDOR-PIPELINE-001` (assurance CLEAR-TO-FREEZE 2026-07-15;
the operator rulings were relayed via the coordinator — not a direct-user
approval in this session): each vendor runs its OWN
:class:`~brutex.dashboard.pull_jobs.BackgroundPullJob` — separate job objects,
separate worker threads, separate progress trackers — so one vendor's halt /
stall / retained terminal never colours the other's.

## The PRIMARY slot is byte-identical (the A/B law)

The primary (Groww) slot is the app's EXISTING single job object, passed in by
IDENTITY — this scaffold constructs NOTHING for the primary vendor, so a
Groww-only registration behaves exactly like today's single-slot model
(pinned: ``job_for(primary) is primary_job``; no feed job object exists until
a feed vendor's slot is actually resolved). The app's routes still consume
their one ``_pull_job`` directly in this pass — wiring the routes through
:meth:`VendorJobSlots.job_for` is the next WS-C commit (deliberately deferred:
the resolution seam + pins land first, route churn second).

## Feed slots FORCE-INJECT the profile bindings (CONC-01)

A feed vendor's job is constructed LAZILY (once, memoised) with the profile's
OWN bindings force-injected — never the Groww ctor defaults (the ADR-0021
Decision 4 force-injection requirement; the profile-level CONC-01 asserts in
:mod:`~brutex.dashboard.vendor_profile` already refused a feed profile
carrying the Groww guard/rate stack, so by the time a slot is built the
bindings are vendor-clean):

* ``market_hours_guard=profile.market_hours_guard`` — the ctor default is the
  GROWW guard (``guard_active_default``); a guard-free feed profile carries
  ``lambda: False`` (CONC-01 — never ``None``);
* ``account_key_resolver=profile.account_key_resolver`` — the ctor default
  resolves the GROWW credential (the ADR-0021 Decision 5 preamble's verified
  inheritance hazard: a job passing NO resolver takes the GROWW account
  flock);
* ``lake_root_resolver`` → the profile's FEED ROOT (``lock_domain.feed_root``)
  — so the job's cross-process flock + lease land under the vendor's own
  ``feeds/<vendor>/.locks``, per-domain by construction (the ADR-0021 §2.5
  sidecar disposition: every feed sidecar nests under the feed subtree).

## Honest envelope

Scaffold ONLY: no route resolves a feed slot in this pass; the observability
singletons (rate panel / pull-rate meter / health-hub pull lane) stay keyed on
the PRIMARY job — a feed slot registers into NONE of them (the ADR-0021
Decision 5 observability row's WS-D pin: registering would blend/evict the
Groww readout; per-vendor surfaces land in WS-E). The feed slot's visibility
is its own job snapshot + structlog events.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from brutex.dashboard.vendor_profile import (
    VendorProfileError,
    VendorProfileRegistry,
    VendorPullProfile,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (no runtime edge)
    from pathlib import Path

    from brutex.dashboard.pull_jobs import BackgroundPullJob

#: A factory building a feed vendor's job FROM its profile (the test seam;
#: the default constructs a real :class:`BackgroundPullJob` with the
#: force-injected bindings — see :func:`_default_feed_job_factory`).
FeedJobFactory = Callable[[VendorPullProfile], "BackgroundPullJob"]


def _default_feed_job_factory(profile: VendorPullProfile) -> BackgroundPullJob:
    """Construct a feed vendor's ``BackgroundPullJob`` with force-injected bindings.

    The profile-level CONC-01 asserts guarantee *profile* is a validated FEED
    profile here (own guard, own resolver, a ``feed_root`` on its lock
    domain), so every Groww ctor default is overridden — the three bindings
    the ADR-0021 force-injection requirement names (guard, account key, lock
    root). The job's flock/lease factories stay the COMMON machinery — they
    key on the resolved root, which IS the feed root, so the lock files land
    per-domain (`feeds/<vendor>/.locks`).
    """
    from brutex.dashboard.pull_jobs import BackgroundPullJob

    feed_root = profile.lock_domain.feed_root
    if feed_root is None:  # defensive totality — the profile asserts refuse this shape
        raise VendorProfileError(
            f"feed profile {profile.vendor!r} carries no feed_root — cannot construct "
            "its job slot (ADR-0021 Decision 5 lock row)"
        )

    def _feed_root_resolver(root: Path = feed_root) -> Path:
        return root

    return BackgroundPullJob(
        market_hours_guard=profile.market_hours_guard,
        account_key_resolver=profile.account_key_resolver,
        lake_root_resolver=_feed_root_resolver,
    )


class VendorJobSlots:
    """The per-vendor job-slot map (ADR-0021 Decision 5 job-slot row).

    Parameters
    ----------
    registry
        The :class:`~brutex.dashboard.vendor_profile.VendorProfileRegistry`
        holding every registered vendor (the primary profile REQUIRED — a
        registry with no primary is refused at construction, fail-loud).
    primary_job
        The app's EXISTING primary ``BackgroundPullJob`` — adopted by
        IDENTITY, never wrapped/reconstructed (the A/B byte-identity law).
    feed_job_factory
        Test seam building a feed vendor's job from its profile; the default
        force-injects the profile bindings (:func:`_default_feed_job_factory`).
    """

    def __init__(
        self,
        *,
        registry: VendorProfileRegistry,
        primary_job: BackgroundPullJob,
        feed_job_factory: FeedJobFactory = _default_feed_job_factory,
    ) -> None:
        self._registry = registry
        self._primary_vendor = registry.primary().vendor
        self._feed_job_factory = feed_job_factory
        #: The slot map. The PRIMARY slot is seeded with the adopted job by
        #: identity; feed slots are constructed LAZILY on first resolve
        #: (a registered-but-never-pulled feed vendor costs nothing).
        self._jobs: dict[str, BackgroundPullJob] = {self._primary_vendor: primary_job}

    @property
    def primary_vendor(self) -> str:
        """The primary profile's vendor token (the adopted single-slot job's)."""
        return self._primary_vendor

    def vendors(self) -> tuple[str, ...]:
        """Every REGISTERED vendor token (registration order — deterministic)."""
        return self._registry.vendors()

    def job_for(self, vendor: str) -> BackgroundPullJob:
        """Resolve *vendor*'s job slot — lazily constructing a feed slot once.

        The primary vendor resolves the adopted job by IDENTITY (byte-identical
        single-slot behavior under a primary-only registration — the A/B pin).
        An unknown vendor is the registry's typed refusal.
        """
        profile = self._registry.resolve(vendor)
        job = self._jobs.get(profile.vendor)
        if job is None:
            job = self._feed_job_factory(profile)
            self._jobs[profile.vendor] = job
        return job

    def constructed_vendors(self) -> tuple[str, ...]:
        """The vendors whose slots EXIST (primary + lazily-built feeds) — for pins."""
        return tuple(self._jobs)


__all__ = [
    "FeedJobFactory",
    "VendorJobSlots",
]
