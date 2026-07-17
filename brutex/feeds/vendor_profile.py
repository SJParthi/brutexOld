"""The ``VendorPullProfile`` abstraction + the vendor-profile REGISTRY.

DEC-FEED-REGISTRY-COMMON-001 (MIGRATION.md #1903) — the COMMON N-feed
registry home. This module is the ADOPTION-WITH-RELOCATION of PR #1570's
``brutex/dashboard/vendor_profile.py`` (DEC-VENDOR-PROFILE-REGISTRY-001,
on-branch ledger #1833 — supersedes-on-land; PR #1570 is still OPEN in the
orchestration wave, final wording to be coordinated with that lane before
landing), re-homed under the leaf ``brutex.feeds`` package (the home the
feed-vendor track's DEC-FEED-INTERFACE-001 (#1782, PR #1482/#1492) chose —
its static-dict registry is superseded-in-part by THIS instance registry;
its ``FeedBinding``/``feed_root`` transport survives on its own track).
Implements ADR-0021 Decision 4 / Decision 5 / §12's ``VendorPullProfile``
sketch per the frozen ADR-0021 design contract (the operator rulings were
relayed via the coordinator — not a direct-user approval in this session).

## What a profile IS (ADR-0021 Decision 4)

A FROZEN per-vendor declaration the staged-chain BINDER consumes. The
orchestration layer never re-mints adapter facts — every field either NAMES an
existing production binding (the Groww instance below is built from the live
constants/callables, imported not copied) or declares a capability's ABSENCE
(an absent capability produces NO staged callable ⇒ no stage node — the
capability-driven-graph law with no graph engine). The registry is the ONE
home for registered profiles: explicit register/resolve, typed fail-loud
refusals, deterministic (registration-order) iteration.

## CONC-01 — the fail-loud construction assertions (ADR-0021 Decision 4's
## FORCE-INJECTION requirement + §17 WS-C)

The Groww-defaulting constructors are an injection hazard (verified on this
tree): ``SessionRateController``'s default factory hardcodes the Groww
8 r/s + 500/min stack (``pull_controller._build_live_controller``) and
``BackgroundPullJob`` defaults the GROWW market-hours guard
(``market_hours_guard=guard_active_default``). A profile therefore
FORCE-INJECTS both — and construction/registration RAISES the typed
:class:`VendorProfileError` when:

* a NON-primary vendor carries ``guard_active_default`` as its guard, or a
  rate factory that resolves ``_build_live_controller`` (checked by identity
  at the two literal spellings AND by a one-shot factory-product probe at
  registration — a feed vendor missing its own rate stack must never
  silently inherit the Groww one);
* two registered profiles share a rate-controller factory (object identity)
  or a factory PRODUCT (the registration-time probe calls each factory once
  and compares products by identity — a shared/singleton product would blend
  the two vendors' rate budgets, ADR-0021 Decision 5 rate row). HONEST
  limit: key-VALUE disjointness of the ACCOUNT locks is behavioral (the
  resolvers may hit AWS and are never called at registration — WS-D
  precondition P1 owns the second vendor's client-id-keyed resolver); the
  registry refuses the resolver-IDENTITY sharing it can see;
* two registered profiles' write domains overlap (the primary excludes the
  entire ``feeds/`` prefix STRUCTURALLY by type — ``WriteDomain.primary``;
  two feed profiles must claim prefix-DISJOINT subtrees), or their domains
  anchor on different resolved lake roots (ADR-0021 Decision 1: ONE physical
  lake root), or a second PRIMARY profile is registered;
* the profile's ``daily_source`` disagrees with its capability set's (one
  declaration, two spellings per the ADR §12 sketch — a silent mismatch
  would bind ``run_spot_daily`` off one field and gate it off the other);
* the lock domain is incoherent (a primary profile must carry the
  account+lake-root+lease trio; a feed profile must carry its own feed-root
  flock — ADR-0021 Decision 5 lock row);
* a NON-primary profile sets ``no_intent_persist=False`` — the v1 ruling
  (ADR-0021 §2.5 ISOL-02 + NG-2): a feed vendor's chain runs with NO
  primary intent store; a future feed resume-intent gets its OWN store
  under its feed subtree via a NEW decision, never the primary singleton.

## NG-2 — the launcher intent gate (ADR-0021 §2.5 ``.pull_intent`` row,
## fold #4 NG2-EDGE)

:func:`launch_intent_store` is the ONE gating seam: a ``no_intent_persist``
profile's launch binds NO primary intent store — the shared launcher then
receives ``pull_intent_store=None`` and STRUCTURALLY skips the primary
``launch_mutex`` entry + the persist + the clear/restore hooks + the banner
(gating only the persist CALL was refuted at fold #4: the launcher enters the
primary store's ``launch_mutex`` on ``pull_intent_store is not None``, held
across ``start()`` including ~10 s of SSM/lease I/O — a feed launch must
never CONTEND on it). The primary profile's launch binds the primary store
unchanged (identity — byte-identical wiring).

## Honest envelope

This module ships the ABSTRACTION + the Groww instance. The FIRST production
consumer is the nightly/CLI headless chain
(:func:`brutex.nightly.pipeline_binding.build_headless_pipeline` resolves
``registry.primary()`` via :func:`brutex.feeds.registry_build.build_default_registry`
— default-inert, report-dict parity pinned); the dashboard ``/pull-pipeline``
route wiring stays the single-slot Groww shape (named follow-up — its body is
pragma-no-cover). A SECOND live vendor ships NOWHERE by default: registering
one is explicit caller config (``extra_profiles``), and any real second-vendor
profile is OPERATOR-GATED on credentials + a servability probe. The staged-chain
binder that constructs a vendor's graph FROM its profile is the next WS-C
commit, and the second vendor's concrete profile is the feed-vendor track's
(WS-D) — this tree ships no feed profile (tests use a synthetic one). The
:meth:`VendorPullProfile.assert_write_dest` enforcement hook is a SEAM only:
NO production writer consults it in this pass — the caller binding + the
profile-free masquerade pin are the GATED follow-up commit (pending explicit
user approval on the WS-B containment CLEAR — the permission boundary this
session recorded; the binding site is marked in
:mod:`brutex.data.parquet_writer`). The ADR §5 ownership-brokering item (the
feed track's ``feeds/{registry,binding}`` layer) resolves toward shape (a) —
this abstraction is the home, their layer the concrete instance; their branch
is not visible from this tree, so the inspection is recorded as pending WS-D
(PR-body record).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Final, Literal

from brutex.config.feed_scope import note_feed_registered
from brutex.data.write_domain import FEEDS_COMPONENT, WriteDomain, primary_write_domain
from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (no runtime edge)
    from pathlib import Path

    from brutex.dashboard.pull_intent import PullIntentStore

#: The primary vendor's token (the Groww-exclusive primary tree, ADR-0021
#: Decision 4 table row 1).
PRIMARY_VENDOR: Final[str] = "groww"

#: ``VendorCapability.options_shape`` vocabulary (ADR-0021 §12 one-line sketch).
OPTIONS_PER_CONTRACT: Final[str] = "per_contract"
OPTIONS_ROLLING_ATM: Final[str] = "rolling_atm"

#: ``LockDomain.kind`` vocabulary (ADR-0021 §12 one-line sketch).
LOCK_TRIO_AT_PRIMARY_ROOT: Final[str] = "trio_at_primary_root"
LOCK_FEED_ROOT_FLOCK: Final[str] = "feed_root_flock"

#: ``TodayCompletenessBinding.kind`` vocabulary (ADR-0021 §15 P3).
TODAY_CLASSIFIER_GATE: Final[str] = "classifier_gate"
TODAY_CLAMP_TO_PREV_SESSION: Final[str] = "clamp_to_prev_session"

#: ``IncrementalityCapability.kind`` vocabulary (ADR-0021 §12 one-line sketch).
INCR_DURABLE_LEDGER: Final[str] = "durable_ledger"
INCR_LAKE_TAIL_SCAN: Final[str] = "lake_tail_scan"


class VendorProfileError(BruteXError):
    """A vendor-profile construction/registration/resolve invariant failed.

    The CONC-01 typed refusal family (ADR-0021 Decision 4 force-injection +
    §17 WS-C): raised at CONSTRUCTION or REGISTRATION time — never mid-pull —
    so a mis-bound vendor chain can never start. A caller receiving this must
    fix the profile wiring; retrying the same registration is a construction
    bug, not a transient.
    """


@dataclass(frozen=True, slots=True)
class VendorCapability:
    """A vendor's frozen capability declaration (ADR-0021 §12 one-line sketch).

    ``{spot, futures, options_shape, metadata, daily_source}`` — an absent
    capability (``False`` / ``None``) produces NO staged callable ⇒ no stage
    node on that vendor's graph (the capability-driven law, Decision 2).
    """

    spot: bool
    futures: bool
    options_shape: Literal["per_contract", "rolling_atm"] | None
    metadata: bool
    daily_source: Literal["native", "derived"]


@dataclass(frozen=True, slots=True)
class PagingProfile:
    """The adapter-owned chunk/tile law: ``max_days_per_request`` per phase kind.

    ADR-0021 §12 one-line sketch. The profile DECLARES the law for the
    binder's planning surfaces; the adapter's own chunk planner (for Groww,
    :mod:`brutex.adapters.groww.chunks` — the interval-tier law) remains the
    executing authority — this declaration is built FROM its constants,
    never re-minted.
    """

    #: ``(phase_kind, max_days_per_request)`` pairs, e.g. ``("spot", 30)``.
    max_days_per_request: tuple[tuple[str, int], ...]

    def days_for(self, phase_kind: str) -> int:
        """Return the declared per-request day cap for *phase_kind* (fail-loud)."""
        for kind, days in self.max_days_per_request:
            if kind == phase_kind:
                return days
        raise VendorProfileError(
            f"paging profile declares no phase kind {phase_kind!r} "
            f"(declared: {[k for k, _ in self.max_days_per_request]})"
        )


@dataclass(frozen=True, slots=True)
class LockDomain:
    """The vendor's lock recipe (ADR-0021 §12 one-line sketch / Decision 5).

    ``trio_at_primary_root`` — the account + lake-root + lease trio at the
    primary root (the shipped Groww shape); ``feed_root_flock`` — the ONE
    feed-root flock at the vendor's own subtree (*feed_root* REQUIRED; the
    per-domain lock disjointness of Decision 1 rationale (c)).
    """

    kind: Literal["trio_at_primary_root", "feed_root_flock"]
    feed_root: Path | None = None


@dataclass(frozen=True, slots=True)
class TodayCompletenessBinding:
    """HOW the per-vendor today-completeness law holds (ADR-0021 §15 P3).

    ``classifier_gate`` — the DEC-PULL-TODAY-COMPLETENESS-GATE-001 classifier
    armed at the vendor's spine (the shipped Groww shape);
    ``clamp_to_prev_session`` — the feed-vendor v1 window-END clamp over
    EVERY window (the native-daily window included — NG-1), whose mechanism
    lands with the WS-D binding (this field is the DECLARATION the binder
    consults; no clamp code ships here).
    """

    kind: Literal["classifier_gate", "clamp_to_prev_session"]


@dataclass(frozen=True, slots=True)
class IncrementalityCapability:
    """HOW ``incremental_from`` is derived (ADR-0021 §12 one-line sketch).

    ``durable_ledger`` (with its ledger *markets*) or ``lake_tail_scan``.
    A vendor with NO incrementality capability carries ``None`` on the
    profile instead — auto-arm is then REFUSED (ADR-0021 Decision 7).
    """

    kind: Literal["durable_ledger", "lake_tail_scan"]
    markets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnderlyingScope:
    """One admitted underlying in a vendor's scope DECLARATION.

    DEC-UNIVERSE-INDEX-ONLY-001 (MIGRATION.md #1934) — the profile-level
    declaration half of the scope law: ``underlying`` is the bare symbol
    (``NIFTY`` / ``INDIAVIX``); ``fno_chain`` declares whether the vendor
    pulls the underlying's derivative chain (``False`` for a reference-only
    spot like INDIAVIX). The declaration is exchange-less by design — the
    ADMISSION authority stays :mod:`brutex.config.feed_scope`'s
    exchange-keyed :class:`~brutex.config.feed_scope.FeedScope`; a mirror-pin
    test keeps the two in agreement for the Groww binding.
    """

    underlying: str
    fno_chain: bool


@dataclass(frozen=True, slots=True)
class FeedScopeBinding:
    """A vendor profile's frozen universe-scope declaration (#1934).

    ``admitted`` enumerates the underlyings the vendor's chain acts on. The
    binding is a DECLARATION the orchestration layer can read off the profile
    — admission predicates consult :func:`brutex.config.feed_scope.resolve_feed_scope`
    (env-overridable), never this tuple, so the two never fight: the binding
    documents the vendor's shipped default, the env law decides at runtime.
    """

    admitted: tuple[UnderlyingScope, ...]


#: The Groww (and default) index4 scope binding — the 4 ``V1_INDICES``
#: underlyings, F&O chains on the 3 brute-force indices only. Mirror-pinned to
#: ``feed_scope.INDEX4_UNDERLYINGS`` / ``INDEX4_FNO_UNDERLYINGS`` by
#: ``test_feeds_vendor_profile_scope.py`` (drift on either side fails CI).
INDEX4_SCOPE_BINDING: Final[FeedScopeBinding] = FeedScopeBinding(
    admitted=(
        UnderlyingScope(underlying="NIFTY", fno_chain=True),
        UnderlyingScope(underlying="BANKNIFTY", fno_chain=True),
        UnderlyingScope(underlying="SENSEX", fno_chain=True),
        UnderlyingScope(underlying="INDIAVIX", fno_chain=False),
    )
)


@dataclass(frozen=True, slots=True)
class VendorPullProfile:
    """One vendor's FROZEN pull-orchestration declaration (ADR-0021 Decision 4).

    Field-for-field the ADR §12 sketch; the per-field owners (adapter vs
    orchestration vs operator ruling) are the Decision 4 table's. The
    construction asserts below are the CONC-01 profile-level arms — the
    cross-profile arms (shared factories/products, domain overlap) live in
    :meth:`VendorProfileRegistry.register`.
    """

    vendor: str
    write_domain: WriteDomain
    capabilities: VendorCapability
    daily_source: Literal["native", "derived"]
    history_floor: Callable[[date], date]
    paging: PagingProfile
    #: ONE rate-controller stack per chain (ADR-0021 §12 invariant 2). Typed
    #: at ``object`` deliberately: the product's concrete type
    #: (``SessionRateController`` for Groww) lives in
    #: :mod:`~brutex.dashboard.pull_controller`, which this module must not
    #: import at module level (the Groww builder lazy-imports it — the
    #: profile layer never becomes an import hub over the heavy spines).
    rate_factory: Callable[[], object]
    quota_factory: Callable[[], object] | None
    #: Adapter-owned reauth machinery — OPAQUE to the orchestration layer
    #: (the ADR §12 sketch deliberately leaves this field's type open).
    reauth_binding: object | None
    account_key_resolver: Callable[[], str | None]
    lock_domain: LockDomain
    #: NEVER ``None`` — the no-guard binding IS ``lambda: False`` (the
    #: CONC-01 type correction; the job ctor's type is non-Optional).
    market_hours_guard: Callable[[], bool]
    today_completeness: TodayCompletenessBinding
    #: NG-2: ``True`` ⇒ the shared launcher binds NO primary intent store
    #: (see :func:`launch_intent_store`).
    no_intent_persist: bool
    incrementality: IncrementalityCapability | None
    #: A callable over the vendor's Greeks pass returning a gate-compatible
    #: outcome (the COMMON ``GreeksEnrichOutcome`` for Groww) — or ``None``
    #: (capability absent). Opaque here for the same no-import-hub reason.
    greeks_seam: Callable[..., object] | None
    ledger_markets: tuple[str, ...] = field(default=())
    #: The vendor's universe-scope DECLARATION (DEC-UNIVERSE-INDEX-ONLY-001,
    #: #1934) — ``None`` = undeclared (a legacy/synthetic profile; admission
    #: still resolves through ``feed_scope``'s fail-closed default). Appended
    #: STRICTLY LAST + defaulted so every existing positional constructor call
    #: is byte-identical (the ``ledger_markets`` additive precedent).
    scope: FeedScopeBinding | None = None

    def assert_write_dest(self, dest: Path) -> None:
        """The Pin D1-1 enforcement HOOK: assert *dest* is inside THIS vendor's domain.

        Delegates to :func:`brutex.data.write_domain.assert_write_domain` over
        the profile's own :class:`~brutex.data.write_domain.WriteDomain` — the
        typed refusal on a cross-domain / out-of-lake / symlink-escaping dest
        (ADR-0021 §12 invariant 1). SEAM ONLY in this pass: NO production
        writer consults it yet — the caller binding (wiring the vendor-chain
        writers through this hook, plus the profile-free masquerade pin) is
        the GATED follow-up commit, pending explicit user approval on the
        WS-B containment CLEAR (the permission boundary this session
        recorded); the binding site is marked in
        :mod:`brutex.data.parquet_writer`.
        """
        from brutex.data.write_domain import assert_write_domain

        assert_write_domain(dest, self.write_domain)

    def __post_init__(self) -> None:
        if not self.vendor:
            raise VendorProfileError("vendor token must be a non-empty string")
        if self.daily_source != self.capabilities.daily_source:
            raise VendorProfileError(
                f"profile {self.vendor!r}: daily_source {self.daily_source!r} disagrees "
                f"with capabilities.daily_source {self.capabilities.daily_source!r} — one "
                "declaration, two spellings (ADR-0021 §12); a silent mismatch would bind "
                "run_spot_daily off one field and gate it off the other"
            )
        self._assert_scope_binding()
        if self.write_domain.primary:
            if self.lock_domain.kind != LOCK_TRIO_AT_PRIMARY_ROOT:
                raise VendorProfileError(
                    f"primary profile {self.vendor!r} must carry the "
                    f"{LOCK_TRIO_AT_PRIMARY_ROOT!r} lock domain (ADR-0021 Decision 5)"
                )
            if self.lock_domain.feed_root is not None:
                raise VendorProfileError(
                    f"primary profile {self.vendor!r} must not carry a feed_root "
                    "on its lock domain (the trio anchors at the primary root)"
                )
        else:
            self._assert_feed_profile_isolation()

    def _assert_scope_binding(self) -> None:
        """The #1934 scope-declaration construction arms (fail-loud, CONC-01 style)."""
        if self.scope is None:
            return
        seen: set[str] = set()
        for entry in self.scope.admitted:
            if not entry.underlying or not entry.underlying.strip():
                raise VendorProfileError(
                    f"profile {self.vendor!r}: scope binding carries an EMPTY underlying "
                    "token — every UnderlyingScope must name a non-blank symbol "
                    "(DEC-UNIVERSE-INDEX-ONLY-001)"
                )
            if entry.underlying in seen:
                raise VendorProfileError(
                    f"profile {self.vendor!r}: scope binding lists underlying "
                    f"{entry.underlying!r} TWICE — one UnderlyingScope per symbol "
                    "(DEC-UNIVERSE-INDEX-ONLY-001)"
                )
            seen.add(entry.underlying)

    def _assert_feed_profile_isolation(self) -> None:
        """The CONC-01 profile-level arms for a NON-primary (feed) vendor."""
        if self.lock_domain.kind != LOCK_FEED_ROOT_FLOCK or self.lock_domain.feed_root is None:
            raise VendorProfileError(
                f"feed profile {self.vendor!r} must carry the {LOCK_FEED_ROOT_FLOCK!r} "
                "lock domain WITH its feed_root path (ADR-0021 Decision 5 — the "
                "per-domain lock disjointness)"
            )
        if not self.no_intent_persist:
            raise VendorProfileError(
                f"feed profile {self.vendor!r} must set no_intent_persist=True — the v1 "
                "ruling (ADR-0021 §2.5 ISOL-02 + NG-2): a feed chain binds NO primary "
                "intent store; a future feed resume-intent gets its OWN store under its "
                "feed subtree via a NEW decision"
            )
        # Lazy imports (deliberate — PLC0415-exempt house pattern): the two
        # Groww-default hazard spellings live in heavy modules this leaf must
        # not import at module level.
        from brutex.calendars.market_hours import guard_active_default
        from brutex.dashboard.pull_controller import (
            SessionRateController,
            _build_live_controller,  # pyright: ignore[reportPrivateUsage]
        )

        if self.market_hours_guard is guard_active_default:
            raise VendorProfileError(
                f"feed profile {self.vendor!r} carries the GROWW market-hours guard "
                "(guard_active_default) — CONC-01: a non-primary vendor never resolves "
                "the Groww guard; the no-guard binding is `lambda: False`, never the "
                "Groww default (ADR-0021 Decision 4 force-injection)"
            )
        if (
            self.rate_factory is _build_live_controller
            or self.rate_factory is SessionRateController
        ):
            raise VendorProfileError(
                f"feed profile {self.vendor!r} carries the GROWW rate stack "
                "(_build_live_controller / SessionRateController default) — CONC-01: a "
                "non-primary vendor never resolves the Groww 8 r/s + 500/min controller "
                "(ADR-0021 Decision 4 force-injection)"
            )


def _probe_rate_product_is_groww_default(product: object) -> bool:
    """``True`` iff *product* is a ``SessionRateController`` on the Groww default factory.

    The DEEP CONC-01 arm (registration-time, one probe call per profile): a
    feed profile hiding the Groww default behind its own lambda
    (``lambda: SessionRateController()``) escapes the identity checks — the
    probe inspects the PRODUCT. Reads the holder's private ``_factory``
    deliberately (the holder exposes no public factory accessor; the probe is
    a construction-time guard, never a hot path).
    """
    from brutex.dashboard.pull_controller import (
        SessionRateController,
        _build_live_controller,  # pyright: ignore[reportPrivateUsage]
    )

    return (
        isinstance(product, SessionRateController)
        # Friend access (DEC-P0-017 per-site ignore): the holder exposes no
        # public factory accessor — the probe is a construction-time guard.
        and product._factory is _build_live_controller  # pyright: ignore[reportPrivateUsage]
    )


class VendorProfileRegistry:
    """The ONE registered-profile home: register/resolve, typed + deterministic.

    Registration runs the CROSS-profile CONC-01 arms (see the module
    docstring); resolve of an unknown vendor is a typed refusal naming the
    registered tokens; iteration is REGISTRATION order (deterministic — no
    dict/set order dependence beyond insertion, which Python guarantees).
    """

    def __init__(self) -> None:
        self._profiles: dict[str, VendorPullProfile] = {}
        #: Registration-time rate-factory PRODUCT probe results, by vendor —
        #: object identities compared across profiles (CONC-01).
        self._rate_products: dict[str, object] = {}

    def register(self, profile: VendorPullProfile) -> None:
        """Register *profile*, running every cross-profile CONC-01 arm — fail-loud."""
        if profile.vendor in self._profiles:
            raise VendorProfileError(
                f"vendor {profile.vendor!r} is already registered — duplicate "
                "registration refused (one profile per vendor token)"
            )
        product = profile.rate_factory()
        if not profile.write_domain.primary and _probe_rate_product_is_groww_default(product):
            raise VendorProfileError(
                f"feed profile {profile.vendor!r}: its rate factory's PRODUCT is a "
                "SessionRateController on the Groww default (_build_live_controller) — "
                "CONC-01: a non-primary vendor never resolves the Groww rate stack, even "
                "behind its own factory wrapper (ADR-0021 Decision 4 force-injection)"
            )
        for other in self._profiles.values():
            self._assert_disjoint(profile, other, product=product)
        self._profiles[profile.vendor] = profile
        self._rate_products[profile.vendor] = product
        # The §2.5 ACTIVE-feed seam (DEC-UNIVERSE-INDEX-ONLY-001 / #1934):
        # fires ONLY here — where profile state actually LANDS, strictly AFTER
        # every refusal arm above — so a REFUSED registration can never grow
        # ``feed_scope.active_feeds()`` and fold a never-operating feed's
        # pre-laid wide binding into the read union (the seam-ordering law;
        # import direction feeds → config ONLY).
        note_feed_registered(profile.vendor)

    def _assert_disjoint(
        self, profile: VendorPullProfile, other: VendorPullProfile, *, product: object
    ) -> None:
        """The pairwise isolation arms (ADR-0021 Decision 5 zero-cross-impact rows)."""
        if profile.rate_factory is other.rate_factory:
            raise VendorProfileError(
                f"profiles {profile.vendor!r} and {other.vendor!r} share ONE rate-controller "
                "factory — CONC-01: per-vendor rate budgets must never blend "
                "(ADR-0021 Decision 5 rate row)"
            )
        if product is self._rate_products[other.vendor]:
            raise VendorProfileError(
                f"profiles {profile.vendor!r} and {other.vendor!r} share ONE rate-controller "
                "factory PRODUCT (the registration probe returned the same object) — "
                "CONC-01: a shared/singleton controller would blend the two vendors' "
                "budgets (ADR-0021 Decision 5 rate row)"
            )
        if profile.account_key_resolver is other.account_key_resolver:
            raise VendorProfileError(
                f"profiles {profile.vendor!r} and {other.vendor!r} share ONE account-key "
                "resolver — CONC-01: the account single-flight must key per vendor account "
                "(ADR-0021 Decision 5 auth row; key-VALUE disjointness is the WS-D P1 "
                "behavioral half — the registry refuses the identity sharing it can see)"
            )
        if profile.write_domain.resolved_root != other.write_domain.resolved_root:
            raise VendorProfileError(
                f"profiles {profile.vendor!r} and {other.vendor!r} anchor on DIFFERENT "
                f"resolved lake roots ({profile.write_domain.resolved_root} vs "
                f"{other.write_domain.resolved_root}) — ADR-0021 Decision 1: ONE physical "
                "lake root with per-vendor write domains"
            )
        if profile.write_domain.primary and other.write_domain.primary:
            raise VendorProfileError(
                f"profiles {profile.vendor!r} and {other.vendor!r} BOTH claim the PRIMARY "
                "write domain — exactly one primary profile per lake (ADR-0021 Decision 1)"
            )
        # Primary-vs-feed subtree disjointness. The WriteDomain type's
        # whole-`feeds/`-prefix exclusion makes a BUILDER-built pair
        # (`feeds_write_domain` enforces the feeds/ nesting law at
        # construction) structurally disjoint — but a hand-built RAW feed
        # WriteDomain claiming a non-feeds/ subtree (e.g. `bars/`) would
        # silently overlap the primary's real write territory, so the pair
        # is refused HERE too (the hostile-review MINOR-2 fold on
        # DEC-FEED-REGISTRY-COMMON-001 — fails-before-pinned; the compare
        # reuses write_domain.FEEDS_COMPONENT, never a re-minted literal).
        pair = (profile, other) if profile.write_domain.primary else (other, profile)
        primary_side, feed_side = pair
        if primary_side.write_domain.primary and not feed_side.write_domain.primary:
            for parts in feed_side.write_domain.allowed_parts:
                if parts[:1] != (FEEDS_COMPONENT,):
                    raise VendorProfileError(
                        f"feed profile {feed_side.vendor!r} claims write-domain subtree "
                        f"{'/'.join(parts)!r} OUTSIDE the '{FEEDS_COMPONENT}/' component "
                        f"while {primary_side.vendor!r} holds the PRIMARY domain — the "
                        "primary owns everything outside feeds/ (ADR-0021 Decision 1 / "
                        "Pin D1-1; every second-vendor byte nests under feeds/<vendor>/)"
                    )
        # Feed-vs-feed subtree disjointness. Prefix overlap in EITHER
        # direction is a refusal.
        for mine in profile.write_domain.allowed_parts:
            for theirs in other.write_domain.allowed_parts:
                shorter = min(len(mine), len(theirs))
                if mine[:shorter] == theirs[:shorter]:
                    raise VendorProfileError(
                        f"profiles {profile.vendor!r} and {other.vendor!r} claim "
                        f"OVERLAPPING write-domain subtrees ({'/'.join(mine)} vs "
                        f"{'/'.join(theirs)}) — per-vendor domains must be prefix-disjoint "
                        "(ADR-0021 Decision 1 / Pin D1-1)"
                    )

    def resolve(self, vendor: str) -> VendorPullProfile:
        """Return the registered profile for *vendor* — unknown = typed refusal."""
        try:
            return self._profiles[vendor]
        except KeyError:
            raise VendorProfileError(
                f"unknown vendor {vendor!r} — registered: {sorted(self._profiles) or '(none)'}"
            ) from None

    def vendors(self) -> tuple[str, ...]:
        """The registered vendor tokens, in REGISTRATION order (deterministic)."""
        return tuple(self._profiles)

    def profiles(self) -> tuple[VendorPullProfile, ...]:
        """The registered profiles, in REGISTRATION order (deterministic)."""
        return tuple(self._profiles.values())

    def primary(self) -> VendorPullProfile:
        """Return the ONE primary-domain profile — typed refusal when absent."""
        for profile in self._profiles.values():
            if profile.write_domain.primary:
                return profile
        raise VendorProfileError(
            "no PRIMARY profile registered — the primary (Groww) profile anchors the "
            "registry (ADR-0021 Decision 1)"
        )


def launch_intent_store(
    profile: VendorPullProfile, primary_store: PullIntentStore | None
) -> PullIntentStore | None:
    """The NG-2 launcher intent gate: which intent store a *profile*'s launch binds.

    ADR-0021 §2.5 ``.pull_intent`` row + fold #4 NG2-EDGE: a
    ``no_intent_persist`` profile's launch binds NO primary intent store —
    the shared launcher receives ``None`` and STRUCTURALLY skips the primary
    ``launch_mutex`` entry, the persist, the clear/restore hooks and the
    banner (gating only the persist call was REFUTED: the launcher enters
    the mutex on ``pull_intent_store is not None``, held across ``start()``
    including ~10 s of SSM/lease I/O). The primary profile's launch binds
    *primary_store* by IDENTITY — byte-identical wiring. The two-vendor
    crash-resume half this seam carries: a feed launch writes nothing into
    the primary singleton, so a crash+boot resumes ONLY a Groww-persisted
    scope and the feed v1 chain (store-less) resumes nothing.
    """
    return None if profile.no_intent_persist else primary_store


def _groww_history_floor(_today: date) -> date:
    """The Groww depth law: the constant ``GROWW_HISTORY_FLOOR`` (CLAUDE.md §2 Rule 3)."""
    from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR

    return GROWW_HISTORY_FLOOR


def groww_pull_profile(
    lake_root: Path,
    *,
    rate_factory: Callable[[], object] | None = None,
) -> VendorPullProfile:
    """Build the GROWW (primary) profile FROM the existing production bindings.

    Every field is read from the live constants/callables (imported, never
    re-minted — the ADR-0021 Decision 4 owner column): the primary write
    domain over *lake_root*; the full capability set with DERIVED daily
    (DEC-1MINUTE-ONLY-DERIVE-1DAY-001 — Groww-only per the ADR §4.1
    supersession); ``GROWW_HISTORY_FLOOR``; the 1-minute 30-day chunk tier
    (every fetch phase pulls ``1minute`` — the interval-tier planner in
    :mod:`brutex.adapters.groww.chunks` stays the executing authority); the
    production ``SessionRateController`` stack (override via *rate_factory*
    so the app can hand its ONE session holder — one instance per chain);
    the AWS-only account-key resolver; the trio lock domain; the LOCKED
    Groww market-hours guard; the classifier-gate today-completeness
    binding; intent persist ON (``no_intent_persist=False``); durable-ledger
    incrementality over the three Groww ledger markets; the COMMON fenced
    Greeks pass.
    """
    from brutex.adapters.groww.constants import (
        GROWW_FUTURE_COVERAGE_MARKET,
        GROWW_OPTION_COVERAGE_MARKET,
        WINDOW_DAYS_30,
    )
    from brutex.adapters.groww.reauth import SdkRefresher
    from brutex.adapters.ingest_common import GROWW_COVERAGE_MARKET
    from brutex.calendars.market_hours import guard_active_default
    from brutex.dashboard.pull_controller import SessionRateController
    from brutex.dashboard.pull_jobs import (
        _default_account_key_resolver,  # pyright: ignore[reportPrivateUsage]
    )
    from brutex.data.greeks_enrich_pass import run_greeks_enrich_pass

    ledger_markets = (
        GROWW_COVERAGE_MARKET,
        GROWW_FUTURE_COVERAGE_MARKET,
        GROWW_OPTION_COVERAGE_MARKET,
    )
    return VendorPullProfile(
        vendor=PRIMARY_VENDOR,
        write_domain=primary_write_domain(lake_root),
        capabilities=VendorCapability(
            spot=True,
            futures=True,
            options_shape="per_contract",
            metadata=True,
            daily_source="derived",
        ),
        daily_source="derived",
        history_floor=_groww_history_floor,
        paging=PagingProfile(
            # Every Groww fetch phase pulls 1minute (DEC-1MINUTE-ONLY-DERIVE-
            # 1DAY-001) → the 30-day chunk tier on every phase kind.
            max_days_per_request=(
                ("spot", WINDOW_DAYS_30),
                ("futures", WINDOW_DAYS_30),
                ("options", WINDOW_DAYS_30),
            )
        ),
        rate_factory=SessionRateController if rate_factory is None else rate_factory,
        quota_factory=None,  # NO daily quota for Groww backtesting (§2 Rule 6)
        reauth_binding=SdkRefresher,  # the read-only SSM re-read machinery (opaque)
        account_key_resolver=_default_account_key_resolver,
        lock_domain=LockDomain(kind="trio_at_primary_root"),
        market_hours_guard=guard_active_default,  # ON, LOCKED (Decision 4)
        today_completeness=TodayCompletenessBinding(kind="classifier_gate"),
        no_intent_persist=False,
        incrementality=IncrementalityCapability(kind="durable_ledger", markets=ledger_markets),
        greeks_seam=run_greeks_enrich_pass,
        ledger_markets=ledger_markets,
        # The #1934 universe-scope declaration: Groww ships index4 — the 4
        # V1_INDICES underlyings, F&O chains on the 3 brute-force indices only
        # (admission still resolves through feed_scope's env-overridable law).
        scope=INDEX4_SCOPE_BINDING,
    )


__all__ = [
    "INCR_DURABLE_LEDGER",
    "INCR_LAKE_TAIL_SCAN",
    "INDEX4_SCOPE_BINDING",
    "LOCK_FEED_ROOT_FLOCK",
    "LOCK_TRIO_AT_PRIMARY_ROOT",
    "OPTIONS_PER_CONTRACT",
    "OPTIONS_ROLLING_ATM",
    "PRIMARY_VENDOR",
    "TODAY_CLAMP_TO_PREV_SESSION",
    "TODAY_CLASSIFIER_GATE",
    "FeedScopeBinding",
    "IncrementalityCapability",
    "LockDomain",
    "PagingProfile",
    "TodayCompletenessBinding",
    "UnderlyingScope",
    "VendorCapability",
    "VendorProfileError",
    "VendorProfileRegistry",
    "VendorPullProfile",
    "groww_pull_profile",
    "launch_intent_store",
]
