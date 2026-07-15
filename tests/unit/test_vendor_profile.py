"""``VendorPullProfile`` + registry unit battery (DEC-VENDOR-PROFILE-REGISTRY-001).

Covers the WS-C profile layer of ADR-0021 Decision 4 / Decision 5 / §12:

* the frozen profile's construction asserts (daily_source agreement, lock
  coherence, the feed-profile CONC-01 arms — Groww guard / Groww rate stack /
  ``no_intent_persist`` v1);
* the registry's typed refusals (duplicate, unknown, root mismatch, dual
  primary, subtree overlap, shared rate factory / factory PRODUCT / account
  resolver) + deterministic registration-order iteration;
* the NG-2 ``launch_intent_store`` gating seam (feed ⇒ ``None``, primary ⇒
  identity);
* the GROWW profile built from the live production bindings (never re-minted);
* the ``assert_write_dest`` enforcement hook (SEAM only — no production
  consumer; the caller binding is the gated follow-up commit).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
from brutex.calendars.market_hours import guard_active_default
from brutex.dashboard.pull_controller import (
    GROWW_HISTORY_FLOOR,
    SessionRateController,
    _build_live_controller,
)
from brutex.dashboard.pull_intent import PullIntentStore
from brutex.dashboard.pull_jobs import _default_account_key_resolver
from brutex.dashboard.vendor_profile import (
    PRIMARY_VENDOR,
    PagingProfile,
    VendorProfileError,
    VendorProfileRegistry,
    groww_pull_profile,
    launch_intent_store,
)
from brutex.data.greeks_enrich_pass import run_greeks_enrich_pass
from brutex.data.write_domain import WriteDomainError

from tests.fakes.vendor_profiles import (
    FakeFeedQuotaTracker,
    FakeFeedRateController,
    synthetic_feed_profile,
)


class TestGrowwProfile:
    """The Groww instance is built FROM the live production bindings."""

    def test_primary_identity_facts(self, tmp_path: Path) -> None:
        profile = groww_pull_profile(tmp_path)
        assert profile.vendor == PRIMARY_VENDOR
        assert profile.write_domain.primary is True
        assert profile.write_domain.resolved_root == tmp_path.resolve()
        assert profile.daily_source == "derived"
        assert profile.capabilities.spot is True
        assert profile.capabilities.futures is True
        assert profile.capabilities.options_shape == "per_contract"
        assert profile.capabilities.metadata is True
        assert profile.no_intent_persist is False

    def test_production_bindings_are_imported_not_reminted(self, tmp_path: Path) -> None:
        profile = groww_pull_profile(tmp_path)
        # The LOCKED Groww guard + the AWS-only account resolver + the COMMON
        # Greeks pass are the literal production callables (identity — never
        # copies; CLAUDE.md §11 COMMON).
        assert profile.market_hours_guard is guard_active_default
        assert profile.account_key_resolver is _default_account_key_resolver
        assert profile.greeks_seam is run_greeks_enrich_pass
        # The depth law is the constant GROWW_HISTORY_FLOOR (CLAUDE.md §2 Rule 3).
        assert profile.history_floor(date(2026, 7, 15)) == GROWW_HISTORY_FLOOR
        # Every fetch phase pulls 1minute → the 30-day chunk tier (§6 chunk row).
        assert profile.paging.days_for("spot") == 30
        assert profile.paging.days_for("futures") == 30
        assert profile.paging.days_for("options") == 30
        # NO daily quota for Groww backtesting (§2 Rule 6, DEC-GROWW-RATE-PERMIN-001).
        assert profile.quota_factory is None
        assert profile.ledger_markets == ("groww", "groww-future", "groww-option")
        assert profile.incrementality is not None
        assert profile.incrementality.kind == "durable_ledger"
        assert profile.incrementality.markets == profile.ledger_markets
        assert profile.today_completeness.kind == "classifier_gate"
        assert profile.lock_domain.kind == "trio_at_primary_root"
        assert profile.lock_domain.feed_root is None

    def test_default_rate_factory_is_the_production_session_holder(self, tmp_path: Path) -> None:
        profile = groww_pull_profile(tmp_path)
        product = profile.rate_factory()
        assert isinstance(product, SessionRateController)

    def test_rate_factory_override_wins(self, tmp_path: Path) -> None:
        sentinel = FakeFeedRateController  # any zero-arg callable
        profile = groww_pull_profile(tmp_path, rate_factory=sentinel)
        assert profile.rate_factory is sentinel

    def test_paging_unknown_phase_kind_is_typed_refusal(self, tmp_path: Path) -> None:
        profile = groww_pull_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="no phase kind 'live_ticks'"):
            profile.paging.days_for("live_ticks")


class TestProfileConstructionAsserts:
    """The CONC-01 profile-level fail-loud arms (ADR-0021 Decision 4)."""

    def test_empty_vendor_token_refused(self, tmp_path: Path) -> None:
        with pytest.raises(VendorProfileError, match="non-empty"):
            replace(groww_pull_profile(tmp_path), vendor="")

    def test_daily_source_mismatch_refused(self, tmp_path: Path) -> None:
        with pytest.raises(VendorProfileError, match="daily_source 'native' disagrees"):
            replace(groww_pull_profile(tmp_path), daily_source="native")

    def test_primary_with_feed_lock_kind_refused(self, tmp_path: Path) -> None:
        feed = synthetic_feed_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="trio_at_primary_root"):
            replace(groww_pull_profile(tmp_path), lock_domain=feed.lock_domain)

    def test_primary_with_feed_root_on_trio_refused(self, tmp_path: Path) -> None:
        groww = groww_pull_profile(tmp_path)
        bad_lock = replace(groww.lock_domain, feed_root=tmp_path / "feeds" / "x")
        with pytest.raises(VendorProfileError, match="must not carry a feed_root"):
            replace(groww, lock_domain=bad_lock)

    def test_feed_without_feed_root_refused(self, tmp_path: Path) -> None:
        feed = synthetic_feed_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="WITH its feed_root"):
            replace(feed, lock_domain=replace(feed.lock_domain, feed_root=None))

    def test_feed_with_trio_lock_refused(self, tmp_path: Path) -> None:
        feed = synthetic_feed_profile(tmp_path)
        groww = groww_pull_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="feed_root_flock"):
            replace(feed, lock_domain=groww.lock_domain)

    def test_feed_with_intent_persist_on_refused(self, tmp_path: Path) -> None:
        # The v1 ISOL-02/NG-2 ruling: a feed chain binds NO primary intent store.
        with pytest.raises(VendorProfileError, match="no_intent_persist=True"):
            synthetic_feed_profile(tmp_path, no_intent_persist=False)

    def test_feed_with_groww_guard_refused(self, tmp_path: Path) -> None:
        feed = synthetic_feed_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="GROWW market-hours guard"):
            replace(feed, market_hours_guard=guard_active_default)

    @pytest.mark.parametrize("groww_spelling", [_build_live_controller, SessionRateController])
    def test_feed_with_groww_rate_stack_refused(
        self, tmp_path: Path, groww_spelling: object
    ) -> None:
        feed = synthetic_feed_profile(tmp_path)
        with pytest.raises(VendorProfileError, match="GROWW rate stack"):
            replace(feed, rate_factory=groww_spelling)  # type: ignore[arg-type]

    def test_groww_profile_keeps_its_own_defaults_legal(self, tmp_path: Path) -> None:
        # The PRIMARY vendor legitimately resolves guard_active_default + the
        # SessionRateController stack — the CONC-01 arms are feed-only.
        profile = groww_pull_profile(tmp_path)
        assert profile.market_hours_guard is guard_active_default
        assert profile.rate_factory is SessionRateController


class TestRegistry:
    """Register/resolve laws + the cross-profile CONC-01 arms."""

    def test_register_resolve_roundtrip_and_order(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        groww = groww_pull_profile(tmp_path)
        feed = synthetic_feed_profile(tmp_path)
        registry.register(groww)
        registry.register(feed)
        assert registry.resolve("groww") is groww
        assert registry.resolve("testfeed") is feed
        # Deterministic REGISTRATION order — never sorted, never set-order.
        assert registry.vendors() == ("groww", "testfeed")
        assert registry.profiles() == (groww, feed)
        assert registry.primary() is groww

    def test_unknown_vendor_typed_refusal_names_registered(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(groww_pull_profile(tmp_path))
        with pytest.raises(VendorProfileError, match=r"unknown vendor 'nope'.*groww"):
            registry.resolve("nope")

    def test_unknown_vendor_on_empty_registry(self) -> None:
        with pytest.raises(VendorProfileError, match=r"\(none\)"):
            VendorProfileRegistry().resolve("anything")

    def test_no_primary_registered_typed_refusal(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path))
        with pytest.raises(VendorProfileError, match="no PRIMARY profile"):
            registry.primary()

    def test_duplicate_vendor_refused(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(groww_pull_profile(tmp_path))
        with pytest.raises(VendorProfileError, match="already registered"):
            registry.register(groww_pull_profile(tmp_path))

    def test_shared_rate_factory_identity_refused(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        feed = synthetic_feed_profile(tmp_path)
        registry.register(feed)
        other = replace(
            synthetic_feed_profile(tmp_path, vendor="otherfeed"),
            rate_factory=feed.rate_factory,
        )
        with pytest.raises(VendorProfileError, match="share ONE rate-controller factory"):
            registry.register(other)

    def test_shared_rate_product_identity_refused(self, tmp_path: Path) -> None:
        # Two DIFFERENT factories returning the SAME (singleton) product —
        # escapes the factory-identity arm, caught by the registration probe.
        shared = FakeFeedRateController()
        registry = VendorProfileRegistry()
        registry.register(replace(synthetic_feed_profile(tmp_path), rate_factory=lambda: shared))
        other = replace(
            synthetic_feed_profile(tmp_path, vendor="otherfeed"),
            rate_factory=lambda: shared,
        )
        with pytest.raises(VendorProfileError, match="factory PRODUCT"):
            registry.register(other)

    def test_feed_hiding_groww_rate_stack_behind_lambda_refused(self, tmp_path: Path) -> None:
        # The DEEP CONC-01 arm: the Groww default hidden behind the feed's own
        # factory wrapper escapes both identity spellings — the registration
        # PRODUCT probe catches it.
        registry = VendorProfileRegistry()
        masked = replace(
            synthetic_feed_profile(tmp_path),
            # The lambda IS the point: a wrapper-masked Groww default must be
            # caught by the PRODUCT probe, not the class-identity spelling.
            rate_factory=lambda: SessionRateController(),  # noqa: PLW0108
        )
        with pytest.raises(VendorProfileError, match="PRODUCT is a SessionRateController"):
            registry.register(masked)

    def test_feed_own_session_holder_with_own_factory_is_legal(self, tmp_path: Path) -> None:
        # A SessionRateController on the feed's OWN inner factory is fine —
        # the probe refuses only the Groww-default product.
        registry = VendorProfileRegistry()
        own = replace(
            synthetic_feed_profile(tmp_path),
            rate_factory=lambda: SessionRateController(factory=FakeFeedRateController),  # type: ignore[arg-type]
        )
        registry.register(own)
        assert registry.vendors() == ("testfeed",)

    def test_shared_account_key_resolver_refused(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        feed = synthetic_feed_profile(tmp_path)
        registry.register(feed)
        other = replace(
            synthetic_feed_profile(tmp_path, vendor="otherfeed"),
            account_key_resolver=feed.account_key_resolver,
        )
        with pytest.raises(VendorProfileError, match="account-key resolver"):
            registry.register(other)

    def test_different_lake_roots_refused(self, tmp_path: Path) -> None:
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        root_a.mkdir()
        root_b.mkdir()
        registry = VendorProfileRegistry()
        registry.register(groww_pull_profile(root_a))
        with pytest.raises(VendorProfileError, match="DIFFERENT.*resolved lake roots"):
            registry.register(synthetic_feed_profile(root_b))

    def test_two_primary_profiles_refused(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(groww_pull_profile(tmp_path))
        # A distinct rate factory + account resolver so the SHARED-identity
        # arms (checked first) cannot mask the dual-primary refusal.
        second = replace(
            groww_pull_profile(tmp_path),
            vendor="groww2",
            # A fresh-per-call factory (NOT the shared class object) so the
            # shared-identity arms cannot mask the dual-primary refusal.
            rate_factory=lambda: FakeFeedRateController(),  # noqa: PLW0108
            account_key_resolver=lambda: "groww2-account",
        )
        with pytest.raises(VendorProfileError, match="BOTH claim the PRIMARY"):
            registry.register(second)

    @pytest.mark.parametrize(
        ("mine", "theirs"),
        [
            ("feeds/testfeed", "feeds/testfeed"),  # identical subtree
            ("feeds/testfeed/sub", "feeds/testfeed"),  # mine nests under theirs
            ("feeds/testfeed", "feeds/testfeed/sub"),  # theirs nests under mine
        ],
    )
    def test_overlapping_feed_subtrees_refused(
        self, tmp_path: Path, mine: str, theirs: str
    ) -> None:
        from brutex.data.write_domain import feeds_write_domain

        registry = VendorProfileRegistry()
        first = replace(
            synthetic_feed_profile(tmp_path),
            write_domain=feeds_write_domain(tmp_path, theirs),
        )
        registry.register(first)
        second = replace(
            synthetic_feed_profile(tmp_path, vendor="otherfeed"),
            write_domain=feeds_write_domain(tmp_path, mine),
        )
        with pytest.raises(VendorProfileError, match="OVERLAPPING write-domain subtrees"):
            registry.register(second)

    def test_disjoint_feed_subtrees_coexist(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(groww_pull_profile(tmp_path))
        registry.register(synthetic_feed_profile(tmp_path))
        registry.register(synthetic_feed_profile(tmp_path, vendor="otherfeed"))
        assert registry.vendors() == ("groww", "testfeed", "otherfeed")


class TestLaunchIntentStoreGate:
    """The NG-2 gating seam — profile-layer half (the launcher pin is its own suite)."""

    def test_feed_profile_binds_no_store(self, tmp_path: Path) -> None:
        store = PullIntentStore(tmp_path)
        assert launch_intent_store(synthetic_feed_profile(tmp_path), store) is None

    def test_primary_profile_binds_store_by_identity(self, tmp_path: Path) -> None:
        store = PullIntentStore(tmp_path)
        assert launch_intent_store(groww_pull_profile(tmp_path), store) is store

    def test_storeless_app_stays_storeless_on_both_arms(self, tmp_path: Path) -> None:
        assert launch_intent_store(groww_pull_profile(tmp_path), None) is None
        assert launch_intent_store(synthetic_feed_profile(tmp_path), None) is None


class TestAssertWriteDestHook:
    """The Pin D1-1 enforcement hook — SEAM only (no production consumer)."""

    def test_primary_hook_passes_own_tree_and_refuses_feeds(self, tmp_path: Path) -> None:
        profile = groww_pull_profile(tmp_path)
        bars_dest = (
            tmp_path / "bars" / "NSE" / "INDEX" / "NIFTY" / "1minute" / "2024" / "01.parquet"
        )
        profile.assert_write_dest(bars_dest)  # in-domain — no raise
        with pytest.raises(WriteDomainError, match="PRIMARY write domain"):
            profile.assert_write_dest(tmp_path / "feeds" / "testfeed" / "bars" / "x.parquet")

    def test_feed_hook_passes_own_subtree_and_refuses_primary(self, tmp_path: Path) -> None:
        profile = synthetic_feed_profile(tmp_path)
        own = tmp_path / "feeds" / "testfeed" / "bars" / "NSE" / "x.parquet"
        profile.assert_write_dest(own)  # in-domain — no raise
        with pytest.raises(WriteDomainError, match="cross-domain writes are refused"):
            profile.assert_write_dest(tmp_path / "bars" / "NSE" / "x.parquet")


class TestPagingProfile:
    def test_days_for_returns_declared_cap(self) -> None:
        paging = PagingProfile(max_days_per_request=(("spot", 90), ("options", 30)))
        assert paging.days_for("spot") == 90
        assert paging.days_for("options") == 30

    def test_quota_fake_is_per_instance(self) -> None:
        # The synthetic quota factory products are per-call instances (the
        # one-stack-per-chain law's test-side mirror).
        assert FakeFeedQuotaTracker() is not FakeFeedQuotaTracker()
