"""Data-driven DEFAULT vendor-registry construction (the ONE build seam).

DEC-FEED-REGISTRY-COMMON-001 (MIGRATION.md #1903). ``build_default_registry``
is the one place a production chain turns "which feed vendors exist on this
box" into a :class:`~brutex.feeds.vendor_profile.VendorProfileRegistry`:

* **Groww registers by DEFAULT** — it is the PRIMARY vendor (the primary
  write domain anchors the registry; ADR-0021 Decision 1) and the only
  profile that ships in-repo. Its rate values are CARRIED, never re-tuned
  (the 8 r/s ceiling + 500/min governor stack —
  DEC-GROWW-RATE-CEILING-8-002 / DEC-GROWW-RATE-PERMIN-001).
* **Additional vendors come ONLY from explicit caller config** — the
  ``extra_profiles`` builder sequence. NO second vendor is ever hardcoded
  here; a real second-vendor profile is OPERATOR-GATED (credentials + a
  servability probe) and lives on its own track.
* **Junk config fails LOUD** — a builder that returns anything but a
  :class:`~brutex.feeds.vendor_profile.VendorPullProfile` is a typed
  :class:`~brutex.feeds.vendor_profile.VendorProfileError`; every
  registration refusal (duplicate token, overlapping write domains, second
  primary, shared rate/account stacks — CONC-01) propagates typed out of
  the build, never a silent skip.

An env/file-driven vendor-list loader is deliberately NOT in v1 (no new env
parsing surface) — named follow-up ``DEC-FEED-REGISTRY-CONFIG-002``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brutex.feeds.vendor_profile import (
    VendorProfileError,
    VendorProfileRegistry,
    VendorPullProfile,
    groww_pull_profile,
)

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (no runtime edge)
    from collections.abc import Callable, Sequence
    from pathlib import Path

#: A caller-config vendor entry: called with the ONE resolved lake root,
#: returns that vendor's frozen profile (registered as-is — fail-loud).
type ProfileBuilder = Callable[[Path], VendorPullProfile]

__all__ = ["ProfileBuilder", "build_default_registry"]


def build_default_registry(
    lake_root: Path,
    *,
    rate_factory: Callable[[], object] | None = None,
    extra_profiles: Sequence[ProfileBuilder] = (),
) -> VendorProfileRegistry:
    """Build the default registry: Groww (primary) + explicit extras only.

    *rate_factory* threads the caller's ONE ``SessionRateController`` holder
    into the Groww profile (ADR-0021 §12 invariant 2 — one rate stack per
    chain); ``None`` keeps the profile's own default factory. Each
    *extra_profiles* builder is called with *lake_root* and its product
    registered verbatim — a non-profile product or any registration refusal
    raises the typed :class:`VendorProfileError` (fail-loud at build; the
    registry never half-registers — refusals fire BEFORE state lands).
    """
    registry = VendorProfileRegistry()
    registry.register(groww_pull_profile(lake_root, rate_factory=rate_factory))
    for builder in extra_profiles:
        profile = builder(lake_root)
        if not isinstance(profile, VendorPullProfile):
            raise VendorProfileError(
                f"extra_profiles builder {builder!r} returned "
                f"{type(profile).__name__!r} — a ProfileBuilder must return a "
                "VendorPullProfile (junk config fails loud)"
            )
        registry.register(profile)
    return registry
