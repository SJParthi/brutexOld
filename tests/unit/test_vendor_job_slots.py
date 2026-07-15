"""``VendorJobSlots`` unit battery (DEC-VENDOR-PROFILE-REGISTRY-001, WS-C).

The per-vendor job-slot scaffold of ADR-0021 Decision 5's job-slot row:

* the PRIMARY slot is the app's existing job by IDENTITY — the A/B
  byte-identity law (a Groww-only registration behaves exactly like today's
  single-slot model: same object, no feed job constructed);
* a feed vendor's slot is constructed LAZILY, ONCE, with the profile's
  bindings FORCE-INJECTED (guard / account-key resolver / feed-root lock
  root) — never the Groww ctor defaults;
* unknown vendors are the registry's typed refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from brutex.calendars.market_hours import guard_active_default
from brutex.dashboard.pull_jobs import BackgroundPullJob, _default_account_key_resolver
from brutex.dashboard.vendor_job_slots import VendorJobSlots, _default_feed_job_factory
from brutex.dashboard.vendor_profile import (
    VendorProfileError,
    VendorProfileRegistry,
    VendorPullProfile,
    groww_pull_profile,
)

from tests.fakes.vendor_profiles import synthetic_feed_profile


@pytest.fixture
def primary_job() -> BackgroundPullJob:
    # A real job object with test-inert lock wiring (no cross-process flock).
    return BackgroundPullJob(lake_root_resolver=lambda: None)


def _registry(tmp_path: Path, *, with_feed: bool = False) -> VendorProfileRegistry:
    registry = VendorProfileRegistry()
    registry.register(groww_pull_profile(tmp_path))
    if with_feed:
        registry.register(synthetic_feed_profile(tmp_path))
    return registry


class TestPrimarySlotByteIdentity:
    """The A/B law: a Groww-only registration IS today's single-slot model."""

    def test_primary_resolves_the_adopted_job_by_identity(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        slots = VendorJobSlots(registry=_registry(tmp_path), primary_job=primary_job)
        assert slots.job_for("groww") is primary_job
        assert slots.primary_vendor == "groww"

    def test_groww_only_registration_constructs_no_feed_job(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        built: list[VendorPullProfile] = []

        def _spy_factory(profile: VendorPullProfile) -> BackgroundPullJob:
            built.append(profile)  # pragma: no cover — must never run
            return BackgroundPullJob()  # pragma: no cover

        slots = VendorJobSlots(
            registry=_registry(tmp_path), primary_job=primary_job, feed_job_factory=_spy_factory
        )
        assert slots.job_for("groww") is primary_job
        assert built == []
        assert slots.constructed_vendors() == ("groww",)

    def test_registered_but_unresolved_feed_costs_nothing(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        built: list[str] = []
        slots = VendorJobSlots(
            registry=_registry(tmp_path, with_feed=True),
            primary_job=primary_job,
            feed_job_factory=lambda p: built.append(p.vendor) or BackgroundPullJob(),  # type: ignore[func-returns-value, return-value]
        )
        assert slots.vendors() == ("groww", "testfeed")
        assert slots.job_for("groww") is primary_job
        # Registration alone constructs nothing (lazy law).
        assert built == []
        assert slots.constructed_vendors() == ("groww",)


class TestFeedSlots:
    def test_feed_slot_lazily_constructed_once_and_memoised(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        calls: list[str] = []

        def _factory(profile: VendorPullProfile) -> BackgroundPullJob:
            calls.append(profile.vendor)
            return BackgroundPullJob(lake_root_resolver=lambda: None)

        slots = VendorJobSlots(
            registry=_registry(tmp_path, with_feed=True),
            primary_job=primary_job,
            feed_job_factory=_factory,
        )
        first = slots.job_for("testfeed")
        second = slots.job_for("testfeed")
        assert first is second
        assert calls == ["testfeed"]
        assert first is not primary_job
        assert slots.constructed_vendors() == ("groww", "testfeed")

    def test_unknown_vendor_is_typed_refusal(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        slots = VendorJobSlots(registry=_registry(tmp_path), primary_job=primary_job)
        with pytest.raises(VendorProfileError, match="unknown vendor 'nope'"):
            slots.job_for("nope")

    def test_registry_without_primary_refused_at_construction(
        self, tmp_path: Path, primary_job: BackgroundPullJob
    ) -> None:
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path))
        with pytest.raises(VendorProfileError, match="no PRIMARY profile"):
            VendorJobSlots(registry=registry, primary_job=primary_job)


class TestDefaultFeedJobFactory:
    """The force-injected bindings (CONC-01 — never the Groww ctor defaults)."""

    def test_force_injects_profile_bindings(self, tmp_path: Path) -> None:
        profile = synthetic_feed_profile(tmp_path)
        job = _default_feed_job_factory(profile)
        # The three ADR-0021 force-injection bindings, asserted on the REAL
        # job object's wiring (private-attr reads — a construction pin, not
        # an API):
        assert job._market_hours_guard is profile.market_hours_guard
        assert job._market_hours_guard is not guard_active_default
        assert job._account_key_resolver is profile.account_key_resolver
        assert job._account_key_resolver is not _default_account_key_resolver
        resolved_root = job._lake_root_resolver()
        assert resolved_root == tmp_path / "feeds" / "testfeed"

    def test_feed_root_missing_is_defensive_typed_refusal(self, tmp_path: Path) -> None:
        # The profile asserts refuse this shape at construction; the factory's
        # totality arm is pinned via a bypassed (object.__new__-free) replace
        # on the frozen lock domain — construct a legal profile, then hand the
        # factory a hand-built twin whose feed_root is None WITHOUT running
        # __post_init__ (object.__setattr__ on the frozen dataclass).
        profile = synthetic_feed_profile(tmp_path)
        object.__setattr__(profile.lock_domain, "feed_root", None)
        with pytest.raises(VendorProfileError, match="no feed_root"):
            _default_feed_job_factory(profile)
