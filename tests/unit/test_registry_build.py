# pyright: reportPrivateUsage=none
"""Tests for `brutex.feeds.registry_build` (DEC-FEED-REGISTRY-COMMON-001).

MIGRATION.md #1903: the ONE data-driven default-registry build seam — Groww
registers by DEFAULT (the primary); additional vendors come ONLY from the
explicit ``extra_profiles`` config; junk config fails LOUD (typed).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from brutex.feeds.registry_build import build_default_registry
from brutex.feeds.vendor_profile import (
    PRIMARY_VENDOR,
    VendorProfileError,
    VendorPullProfile,
)

from tests.fakes.vendor_profiles import synthetic_feed_profile

if TYPE_CHECKING:
    from pathlib import Path


class TestDefaultBuild:
    def test_default_registers_exactly_groww(self, tmp_path: Path) -> None:
        """The bare build registers EXACTLY the Groww primary — nothing else."""
        registry = build_default_registry(tmp_path)
        assert registry.vendors() == (PRIMARY_VENDOR,)
        assert registry.primary().vendor == PRIMARY_VENDOR
        assert registry.primary().write_domain.primary is True

    def test_no_second_vendor_ships_by_default(self, tmp_path: Path) -> None:
        """The honest gate: a second vendor NEVER appears without explicit config."""
        registry = build_default_registry(tmp_path)
        with pytest.raises(VendorProfileError, match="unknown vendor"):
            registry.resolve("testfeed")

    def test_rate_factory_override_threads_by_identity(self, tmp_path: Path) -> None:
        """The caller's ONE rate holder IS the Groww profile's factory product."""
        holder = object()
        registry = build_default_registry(tmp_path, rate_factory=lambda: holder)
        assert registry.primary().rate_factory() is holder

    def test_default_rate_factory_when_none(self, tmp_path: Path) -> None:
        """rate_factory=None keeps the profile's own production default factory."""
        from brutex.dashboard.pull_controller import SessionRateController

        registry = build_default_registry(tmp_path)
        assert isinstance(registry.primary().rate_factory(), SessionRateController)


class TestExtraProfiles:
    def test_extra_builder_round_trip(self, tmp_path: Path) -> None:
        """An explicit testfeed builder registers + resolves; Groww stays primary."""
        registry = build_default_registry(tmp_path, extra_profiles=[synthetic_feed_profile])
        assert registry.vendors() == (PRIMARY_VENDOR, "testfeed")
        assert registry.resolve("testfeed").vendor == "testfeed"
        assert registry.primary().vendor == PRIMARY_VENDOR

    def test_extra_builder_receives_the_lake_root(self, tmp_path: Path) -> None:
        """Each builder is called with the ONE resolved lake root."""
        seen: list[Path] = []

        def _builder(lake_root: Path) -> VendorPullProfile:
            seen.append(lake_root)
            return synthetic_feed_profile(lake_root)

        build_default_registry(tmp_path, extra_profiles=[_builder])
        assert seen == [tmp_path]

    def test_refusing_builder_propagates_typed(self, tmp_path: Path) -> None:
        """A registration refusal (dup token) propagates typed out of the build."""

        def _groww_again(lake_root: Path) -> VendorPullProfile:
            from brutex.feeds.vendor_profile import groww_pull_profile

            return groww_pull_profile(lake_root)

        with pytest.raises(VendorProfileError, match="already registered"):
            build_default_registry(tmp_path, extra_profiles=[_groww_again])

    def test_junk_builder_product_fails_loud(self, tmp_path: Path) -> None:
        """A builder returning a non-profile is a typed refusal, never a silent skip."""
        junk = cast(Any, lambda _lake: "not-a-profile")
        with pytest.raises(VendorProfileError, match="must return a VendorPullProfile"):
            build_default_registry(tmp_path, extra_profiles=[junk])

    def test_junk_builder_names_the_product_type(self, tmp_path: Path) -> None:
        junk = cast(Any, lambda _lake: None)
        with pytest.raises(VendorProfileError, match="'NoneType'"):
            build_default_registry(tmp_path, extra_profiles=[junk])

    def test_second_primary_refused(self, tmp_path: Path) -> None:
        """THE named 'second primary' law: exactly ONE primary-domain profile
        may register — a second-primary extra refuses at build (typed)."""
        from dataclasses import replace

        from brutex.feeds.vendor_profile import groww_pull_profile

        from tests.fakes.vendor_profiles import FakeFeedRateController

        def _second_primary(lake_root: Path) -> VendorPullProfile:
            # A distinct token + rate factory + account resolver so the
            # dup/shared-identity arms (checked first) cannot mask the
            # dual-primary refusal.
            return replace(
                groww_pull_profile(lake_root),
                vendor="groww2",
                rate_factory=lambda: FakeFeedRateController(),  # noqa: PLW0108
                account_key_resolver=lambda: "groww2-account",
            )

        with pytest.raises(VendorProfileError, match="BOTH claim the PRIMARY"):
            build_default_registry(tmp_path, extra_profiles=[_second_primary])

    def test_two_extras_register_in_order(self, tmp_path: Path) -> None:
        """Iteration order = registration order (deterministic)."""

        def _other(lake_root: Path) -> VendorPullProfile:
            return synthetic_feed_profile(lake_root, vendor="otherfeed")

        registry = build_default_registry(tmp_path, extra_profiles=[synthetic_feed_profile, _other])
        assert registry.vendors() == (PRIMARY_VENDOR, "testfeed", "otherfeed")
