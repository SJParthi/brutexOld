# pyright: reportPrivateUsage=none
"""Property tests for `brutex.feeds.vendor_profile` (DEC-FEED-REGISTRY-COMMON-001).

MIGRATION.md #1903 — two registry LAWS proven over generated inputs:

1. Any set of prefix-DISJOINT feed write-domains registers successfully in
   ANY order, and iteration is exactly registration order (deterministic).
2. Any prefix-OVERLAPPING pair (identical subtree, or one nesting under the
   other — either direction) refuses at the SECOND registration regardless
   of which of the pair registers first.
"""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from brutex.data.write_domain import feeds_write_domain
from brutex.feeds.vendor_profile import (
    VendorProfileError,
    VendorProfileRegistry,
    VendorPullProfile,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.fakes.vendor_profiles import synthetic_feed_profile

#: One shared lake root for every generated example (the domains all anchor
#: on ONE physical root — ADR-0021 Decision 1; per-example dirs would trip
#: hypothesis' function-scoped-fixture health check for zero extra coverage).
_LAKE_ROOT = Path(tempfile.mkdtemp(prefix="brutex-vendor-prop-"))

_NAME = st.text(alphabet="abcdefgh", min_size=1, max_size=6)


def _feed_profile(
    subtree_name: str, *, vendor: str, nested: tuple[str, ...] = ()
) -> VendorPullProfile:
    """A CONC-01-clean feed profile over ``feeds/<subtree_name>[/nested…]``."""
    subtree = "/".join(("feeds", subtree_name, *nested))
    return replace(
        synthetic_feed_profile(_LAKE_ROOT, vendor=vendor),
        write_domain=feeds_write_domain(_LAKE_ROOT, subtree),
    )


@given(names=st.lists(_NAME, min_size=1, max_size=4, unique=True), data=st.data())
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_disjoint_feed_domains_register_order_independently(
    names: list[str], data: st.DataObject
) -> None:
    """LAW 1: pairwise-distinct single-component subtrees are prefix-disjoint
    (component compare, never string prefix), so EVERY registration order
    succeeds — and ``vendors()`` reads back exactly that order."""
    order = data.draw(st.permutations(names))
    registry = VendorProfileRegistry()
    for index, name in enumerate(order):
        registry.register(_feed_profile(name, vendor=f"feed{index}"))
    assert registry.vendors() == tuple(f"feed{i}" for i in range(len(order)))
    assert len(registry.profiles()) == len(names)


@given(
    name=_NAME,
    extension=st.lists(_NAME, min_size=0, max_size=2),
    swap=st.booleans(),
)
@settings(suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_overlapping_pair_refuses_regardless_of_order(
    name: str, extension: list[str], swap: bool
) -> None:
    """LAW 2: an identical subtree (empty *extension*) or a nested one (either
    direction, via *swap*) is a typed refusal at the SECOND registration."""
    shallow = _feed_profile(name, vendor="feeda")
    deep = _feed_profile(name, vendor="feedb", nested=tuple(extension))
    first, second = (deep, shallow) if swap else (shallow, deep)
    registry = VendorProfileRegistry()
    registry.register(first)
    with pytest.raises(VendorProfileError, match="OVERLAPPING write-domain subtrees"):
        registry.register(second)
    # The refusal fired BEFORE any state landed — the loser is NOT registered.
    assert registry.vendors() == (first.vendor,)
