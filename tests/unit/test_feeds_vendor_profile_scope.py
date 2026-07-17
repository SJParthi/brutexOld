"""The vendor-profile scope binding + the §2.5 registration seam (#1934).

DEC-UNIVERSE-INDEX-ONLY-001: the ``scope: FeedScopeBinding | None`` field
appended STRICTLY LAST on :class:`VendorPullProfile` (positional back-compat),
the ``UnderlyingScope``/``FeedScopeBinding`` shapes + their construction
arms, the Groww profile's index4 binding (mirror-pinned to the feed_scope
sets), and the ``note_feed_registered`` seam firing ONLY where registration
state lands.
"""

from __future__ import annotations

from dataclasses import fields, replace
from pathlib import Path

import pytest

pytest.importorskip("duckdb", reason="the Groww profile chain pulls the data extras")

from brutex.config.feed_scope import (
    INDEX4_FNO_UNDERLYINGS,
    INDEX4_UNDERLYINGS,
    active_feeds,
)
from brutex.feeds.vendor_profile import (
    INDEX4_SCOPE_BINDING,
    FeedScopeBinding,
    UnderlyingScope,
    VendorProfileError,
    VendorProfileRegistry,
    VendorPullProfile,
    groww_pull_profile,
)

from tests.fakes.vendor_profiles import synthetic_feed_profile


class TestScopeBindingShapes:
    def test_scope_field_is_strictly_last_and_defaulted(self) -> None:
        """Positional back-compat: ``scope`` appended LAST, default ``None``."""
        profile_fields = fields(VendorPullProfile)
        assert profile_fields[-1].name == "scope"
        assert profile_fields[-2].name == "ledger_markets"

    def test_synthetic_profile_defaults_scope_none(self, tmp_path: Path) -> None:
        assert synthetic_feed_profile(tmp_path).scope is None

    def test_empty_underlying_refused(self, tmp_path: Path) -> None:
        bad = FeedScopeBinding(admitted=(UnderlyingScope(underlying="  ", fno_chain=True),))
        with pytest.raises(VendorProfileError, match="EMPTY underlying"):
            replace(synthetic_feed_profile(tmp_path), scope=bad)

    def test_duplicate_underlying_refused(self, tmp_path: Path) -> None:
        bad = FeedScopeBinding(
            admitted=(
                UnderlyingScope(underlying="NIFTY", fno_chain=True),
                UnderlyingScope(underlying="NIFTY", fno_chain=False),
            )
        )
        with pytest.raises(VendorProfileError, match="TWICE"):
            replace(synthetic_feed_profile(tmp_path), scope=bad)

    def test_valid_binding_accepted(self, tmp_path: Path) -> None:
        profile = replace(synthetic_feed_profile(tmp_path), scope=INDEX4_SCOPE_BINDING)
        assert profile.scope is INDEX4_SCOPE_BINDING


class TestGrowwBinding:
    def test_groww_profile_carries_index4_binding(self, tmp_path: Path) -> None:
        assert groww_pull_profile(tmp_path).scope is INDEX4_SCOPE_BINDING

    def test_index4_binding_mirrors_feed_scope_sets(self) -> None:
        """The profile DECLARATION ↔ the feed_scope ADMISSION sets (mirror pin).

        The binding is exchange-less by design — symbols compare by NAME; the
        F&O-chain flags mirror the F&O subset exactly (INDIAVIX chainless).
        """
        declared = {entry.underlying for entry in INDEX4_SCOPE_BINDING.admitted}
        assert declared == {symbol for _, symbol in INDEX4_UNDERLYINGS}
        declared_fno = {
            entry.underlying for entry in INDEX4_SCOPE_BINDING.admitted if entry.fno_chain
        }
        assert declared_fno == {symbol for _, symbol in INDEX4_FNO_UNDERLYINGS}


class TestRegistrationSeamOrdering:
    """§2.5 — the seam fires ONLY where ``self._profiles[vendor]`` lands."""

    def test_successful_registration_grows_active(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path, vendor="newfeed"))
        assert "newfeed" in active_feeds()

    def test_duplicate_refusal_never_grows_active(self, tmp_path: Path) -> None:
        registry = VendorProfileRegistry()
        registry.register(synthetic_feed_profile(tmp_path, vendor="onefeed"))
        with pytest.raises(VendorProfileError, match="already registered"):
            registry.register(synthetic_feed_profile(tmp_path, vendor="onefeed"))
        # The FIRST registration landed (active); the refused one grew nothing
        # beyond it — active is exactly {groww seed, onefeed}.
        assert active_feeds() == frozenset({"groww", "onefeed"})

    def test_construction_refusal_never_grows_active(self, tmp_path: Path) -> None:
        """A profile that fails CONSTRUCTION never reaches the seam at all."""
        with pytest.raises(VendorProfileError, match="EMPTY underlying"):
            replace(
                synthetic_feed_profile(tmp_path, vendor="badfeed"),
                scope=FeedScopeBinding(admitted=(UnderlyingScope(underlying="", fno_chain=False),)),
            )
        assert "badfeed" not in active_feeds()
