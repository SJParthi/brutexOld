"""Per-vendor lake WRITE DOMAINS + the Pin D1-1 runtime cross-domain refusal.

DEC-STAGED-SEQUENCER-SEAMS-001 (WS-B of the dual-vendor orchestration track),
implementing ADR-0021 Decision 1 / §2.5 Pin D1-1 / Decision 4's WRITE-DOMAIN
TYPING law per the frozen design contract `DEC-DUAL-VENDOR-PIPELINE-001`
(assurance CLEAR-TO-FREEZE 2026-07-15; the operator rulings were relayed via
the coordinator — not a direct-user approval in this session).

## The law (ADR-0021 Decision 4, the NG-3 typing — the single-``Path`` form
## REFUTED)

A :class:`WriteDomain` is an **ALLOWED-SUBTREE SET with FOREIGN-DOMAIN
EXCLUSION** — never a single root ``Path``: a second vendor's feed subtree
(``feeds/<vendor>/**``) NESTS under the primary lake root, and nested roots
are not disjoint prefixes, so a bare realpath-STARTSWITH check against a
single primary-root ``Path`` FALSE-PASSES a feed-subtree dest presented to
the PRIMARY profile. A dest is in-domain iff its POST-realpath path, relative
to the RESOLVED lake root, falls under one of THIS domain's subtrees AND
under NO OTHER registered domain's subtree — and the PRIMARY domain
additionally excludes the ENTIRE ``feeds/`` path-component prefix
STRUCTURALLY, **registration-INDEPENDENT** ("the root MINUS the whole
``feeds/`` subtree", NEVER the registered-subtrees complement: an
UNREGISTERED ``feeds/<other>/`` dest falls inside that complement, so a
primary-profile write there would FALSE-PASS — the ADR-0021 fold-#4
NG3-EDGE-UNREGISTERED-SUBTREE correction; the primary scanners' zero-
``feeds/``-references invisibility argument already relies on exactly the
whole-prefix form).

:func:`assert_write_domain` is the runtime pin: a cross-domain dest RAISES
the typed :class:`WriteDomainError` — never a keep-first merge (the primary
partition grammar carries no vendor dimension and the keep-first writer does
NOT raise on a cross-domain dest — the stored bar would silently win, an
irreversible provenance loss; ADR-0021 Pin D1-1 "why RUNTIME").

## Path resolution (the DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 precedent — the
## round-2 escape rail + the R5 guarded resolve + the round-6 in-lake
## component-swap rail, all three ported)

The containment test mirrors
:meth:`~brutex.data.option_parquet_writer.OptionParquetWriter._assert_options_containment`:
the lake ROOT is resolved ONCE at registration and the domain's subtree
components are appended LITERALLY — the final path components (``feeds``,
the vendor dir, ``bars``, …) are deliberately NOT symlink-resolved, because
resolving them would re-anchor the containment root to wherever a swapped-in
symlink points (re-opening the hole); the DEST side IS fully resolved
(``dest.resolve()``, guarded — a symlink-loop / unresolvable dest is the
same typed refusal, the R5 fold), so an escape-by-symlink dest is caught
while a lake ROOT reached via a symlink (the legitimate mount shape) still
passes — BOTH sides resolve the root consistently (test-pinned, the
mount-shape fixture). ADDITIONALLY (the precedent's ROUND-6 in-lake swap
rail, ported at the assurance NG4E-2 MAJOR fold): the resolved-path
judgment alone FAILS OPEN when ``<root>/feeds`` itself is swapped to a
symlink — the ``feeds`` token resolves AWAY, so a primary-profile write
addressed under ``feeds/<vendor>/`` would pass the literal-prefix exclusion
while physically landing in the feed vendor's real bytes (empirically
reproduced; the feeds profile simultaneously mis-refused its own tree).
:func:`assert_write_domain` therefore probes the root's ``feeds`` component
with ``is_symlink()`` and refuses any dest whose RESOLVED path sits under
the swapped component's target, naming the symlinked component (fail-closed
both directions; a bars-tree dest away from the swap target still passes).
Honest limit (the precedent's stated envelope, Golden Rule #10): a symlink
swap BETWEEN the rail probe and the actual write inside one call would need
dirfd-relative I/O to close — out of scope, not claimed.

## Case sensitivity (honest documented limit)

Every path-component compare in this module — the ``feeds/`` prefix
exclusion included — is byte/case-SENSITIVE (``rel_parts[:1] ==
(FEEDS_COMPONENT,)``): on a case-INSENSITIVE filesystem (macOS default APFS)
a differently-cased ``Feeds/`` dest aliases the same on-disk directory yet
would NOT be excluded from the primary domain. This is a defense-in-depth
caveat, not a live hole — the production writers only ever mint the
lowercase literal ``feeds`` (:data:`FEEDS_COMPONENT`), so no production dest
carries another casing. The current behavior is deliberately test-pinned
(mixed-case ``FeedS/`` is NOT the feeds subtree); switching to a casefolded
compare would be a behavior change needing its own design pass on
case-insensitive-filesystem semantics, never a silent edit.

## Scoping + the profile-free lane ASSIGNMENTS (NG4E-1 — the domain-less
## arm CLOSED by the masquerade pin, DEC-WRITE-DOMAIN-CALLER-BINDING-001)

The refusal binds **PROFILE-BOUND vendor-chain writers** through
:func:`assert_write_domain`; a profile-FREE
:class:`~brutex.data.parquet_writer.ParquetWriter` now carries the MASQUERADE
pin instead (:func:`assert_profile_free_dest` — a writer without a profile
must not be able to silently write under ``feeds/``; every legitimate
maintenance-lane dest today is outside ``feeds/``, so those lanes stay
byte-identical). The RESTORE lanes stay wholly exempt: the nightly restore
(:mod:`brutex.nightly.restore` — its own verify-everything-then-write law)
and the off-box restic restore (DEC-BACKUP-OFFBOX-001 — whole-root
snapshots) legitimately write across the whole lake root, feed subtrees
included, and never route through :class:`ParquetWriter`; wrapping them in a
per-vendor domain would break whole-lake disaster recovery for zero
isolation gain (the restore runbooks stop the pulls first).

LANE ASSIGNMENTS (the vendor-profile-registry deferral register's item (d)
— PR #1570's on-branch ledger #1833, adopted-with-relocation by
DEC-FEED-REGISTRY-COMMON-001 (MIGRATION.md #1903) — settled here):

* the lake healer (:mod:`brutex.data.lake_heal`) — MAINTENANCE-EXEMPT:
  its scanner walks only the primary ``bars/`` + ``options/`` trees (never
  ``feeds/``), its heal rewrites carry their own containment rails
  (DEC-LAKE-HEAL-OPTIONS-FOREIGN-001), and its 1day recall derive routes
  through the masquerade-covered shared writer;
* the Greeks enrich pass + bulk backfill
  (:mod:`brutex.data.greeks_enrich_pass` / :mod:`brutex.data.greeks_backfill`)
  — MAINTENANCE-EXEMPT today (primary FNO tree, masquerade-covered);
  VENDOR-CHAIN-TO-BE-WIRED when a feed vendor's Greeks binding lands
  (ADR-0021 §17 WS-D / precondition P4 + the Pin D1-3 read twin — that
  vendor's enrich writer is then constructed WITH its feed domain);
* the prebuild timeframe derive (:mod:`brutex.data.timeframe_materialize` +
  :mod:`brutex.data.spot_daily_resample`) — MAINTENANCE-EXEMPT today
  (primary spot+futures trees, masquerade-covered);
  VENDOR-CHAIN-TO-BE-WIRED when a feed vendor's prebuild stage lands
  (ADR-0021 §12.1 — the feed prebuild writer is constructed with the feed
  profile's domain over its feed root);
* the crossverify staging writer (:mod:`brutex.crossverify`) —
  MAINTENANCE-EXEMPT: reads the primary lake and stages CSV bytes under its
  own ``.crossverify/`` staging tree (never a ``feeds/`` writer); its pull
  legs ride the domain-bound live builders.

Honest residual (disclosed, not claimed): the masquerade judgment is
LAKE-RELATIVE — a profile-free writer CONSTRUCTED with its ``lake_root``
already INSIDE a feed subtree mints dests that are ``bars/``-relative to its
OWN root, invisible to this pin. That construction shape is owned by the
profile/slot layer (a feed vendor's writer is constructed WITH its domain —
the force-injection law the per-vendor job-slot layer carries; that layer's
salvage from the superseded PR #1570 stack rides its own PR), and every
primary maintenance lane is constructed at the primary root today.

## Restore ordering across dual window ledgers (the coordinator-relayed WS-B
## obligation NG4E-2 — SETTLED per the operator ruling of 2026-07-15 as
## relayed via the coordinator)

When a second vendor's window ledger lands under its feed subtree (ADR-0021
Pin D1-2), a whole-lake restore involves TWO ledgers. The settled ordering
law, generalizing the shipped single-ledger law ("`ledger.parquet` strictly
LAST" — DEC-SWEEP-NIGHTLY-AUTOMATION-001's restore-order rationale: the
ledger is the pull's skip authority, so it must never claim coverage of bars
that were not yet restored), has three parts:

(a) **Per-domain ledger-strictly-LAST is the MECHANICAL correctness law**:
each write domain restores its own data files FIRST and its window ledger
strictly LAST — that ordering is what makes a partially-restored domain
self-consistent and re-pullable (the ledger never claims coverage of bars
its own domain has not yet restored; a crash mid-restore leaves that
domain's chain conservatively re-verifying via the idempotent keep-first
re-pull, never skipping). The clause CARRIES the per-domain
``window_ledger/deltas/`` clear (the assurance adjudication's required
addendum): the nightly restore's Phase C clears manifest-EXTRANEOUS delta
files BEFORE the base ledger copy so post-snapshot deltas cannot fold
latest-wins over the restored base — generalized per-domain exactly like
the ledger-last clause: each domain clears its OWN deltas before its OWN
base ledger copy, ledger strictly last.

(b) **Primary-ledger-last-overall is a labeled completion-sentinel
CONVENTION, not a correctness requirement**: restoring the PRIMARY ledger
after every other domain gives the operator one "the whole restore
finished" sentinel — useful, but correctness is carried entirely by (a)
within each domain.

(c) **The real guarantee is the QUIESCED-BOX precondition**: restore runs
ONLY on a quiesced box — no pulls in flight in ANY domain (the restore
runbooks stop the pulls first). The ordering laws above reason about
crash-DURING-restore shapes; they never license restoring under a live
vendor chain.

No restore lane consumes this in WS-B (the wiring is WS-C+, alongside Pin
D1-2's backup-allowlist add).

## Consumption (the Pin D1-1 caller binding —
## DEC-WRITE-DOMAIN-CALLER-BINDING-001)

The WS-B "NO writer consults it yet" envelope is CLOSED:
:class:`~brutex.data.parquet_writer.ParquetWriter` consults this module at
its ONE minted-dest chokepoint (``write()``, before any directory/lock/byte
is touched) — a domain-carrying writer runs :func:`assert_write_domain`, a
profile-FREE writer runs the :func:`assert_profile_free_dest` masquerade pin
— and the three live production pull sink builders
(``pull_controller._build_live_spot_adapter`` +
``historical_pull._build_live_client`` + ``cli._build_live_groww_spot``,
the last bound at the hostile-review MINOR-2 fold) bind
:func:`primary_write_domain`.
The binding landed under DIRECT in-session user approval (2026-07-16 — the
operator's "accepted" on the explicit authorization request; the WS-B/WS-C
relayed-authority deferral is thereby resolved). The Pin D1-3 read-side twin
on the Greeks spot-join stays WS-D P4 (ADR-0021 §17). Cost: one guarded
``Path.resolve`` + one ``lstat`` swap-rail probe + O(subtrees) tuple-prefix
compares per asserted dest — O(1) in lake size.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from brutex.data.errors import DataLakeError

#: The ONE feed-root path component (ADR-0021 Decision 1): every second-vendor
#: byte nests under ``<lake_root>/feeds/<vendor>/**``; the PRIMARY domain
#: excludes this ENTIRE path-component prefix structurally.
FEEDS_COMPONENT: Final[str] = "feeds"

#: The primary domain's profile token (the Groww-exclusive primary tree).
PRIMARY_DOMAIN_NAME: Final[str] = "primary"

#: The refusal-message token for a PROFILE-FREE writer (the masquerade pin —
#: DEC-WRITE-DOMAIN-CALLER-BINDING-001; NG4E-1's domain-less-writer arm).
PROFILE_FREE_NAME: Final[str] = "profile-free"

#: A feed subtree must be at least ``feeds/<vendor>`` — two path components
#: (ADR-0021 Decision 1: a bare ``feeds`` claims the whole feed root).
_MIN_FEED_SUBTREE_PARTS: Final[int] = 2


class WriteDomainError(DataLakeError):
    """A staged write dest fell OUTSIDE the active profile's write domain.

    The Pin D1-1 typed refusal (ADR-0021 §2.5): raised by
    :func:`assert_write_domain` for a cross-domain / out-of-lake /
    unresolvable dest — never a keep-first merge, never a silent
    arbitration. A vendor-chain writer receiving this must NOT retry the
    same dest: the binding is mis-rooted (a construction bug the WS-C
    fail-loud assertions exist to catch), not a transient.
    """


def _clean_relative_parts(subtree: str) -> tuple[str, ...]:
    """Validate + split a registration-time subtree token into path parts.

    Registration-time fail-loud (never at assert time): a subtree is a
    RELATIVE, traversal-free POSIX-style token (``feeds/<vendor>`` …).
    Absolute paths, ``..`` components and the empty string are refused
    (bare ``.`` components are normalized away by ``PurePosixPath``) — a
    junk registration must never silently widen or narrow a domain
    (Golden Rule #1).
    """
    pure = PurePosixPath(subtree)
    if pure.is_absolute() or not subtree:
        raise WriteDomainError(
            f"write-domain subtree {subtree!r} must be a non-empty RELATIVE path "
            "(it is joined under the resolved lake root)"
        )
    parts = pure.parts
    # PurePosixPath already normalizes away bare '.' components; '..' is the
    # one traversal token that survives into parts — refused.
    if ".." in parts:
        raise WriteDomainError(
            f"write-domain subtree {subtree!r} must not contain '..' components "
            "(traversal-free registration — the domain is a literal subtree)"
        )
    return parts


@dataclass(frozen=True, slots=True)
class WriteDomain:
    """One profile's lake write domain — the allowed-subtree SET form (NG-3).

    Constructed ONCE at profile registration via :func:`primary_write_domain`
    / :func:`feeds_write_domain` (the lake root is resolved exactly once
    there — never per assert; the ADR §2.5 "profile subtrees resolved ONCE at
    registration" requirement). Fields:

    Attributes
    ----------
    name
        The profile token the refusal message names (``"primary"`` or the
        feed subtree token, e.g. ``"feeds/<vendor>"``).
    resolved_root
        The RESOLVED (realpath) lake root every containment compare anchors
        on. Resolved at registration; the subtree components below are
        appended literally, never re-resolved (the R6 mount-shape law).
    allowed_parts
        The allowed subtrees as relative path-part tuples. EMPTY for the
        PRIMARY domain — its allowance is "the whole root MINUS the entire
        ``feeds/`` prefix" (structural, registration-independent), not an
        enumerable subtree list.
    foreign_parts
        OTHER registered profiles' subtrees (relative part tuples) — the
        explicit foreign-domain exclusion belt. For the primary domain the
        structural ``feeds/`` exclusion already subsumes every REGISTERED
        and UNREGISTERED feed subtree (the fold-#4 NG3-EDGE law); the belt
        exists for future non-``feeds/`` domain shapes and for feeds-profile
        vs feeds-profile disjointness documentation.
    primary
        ``True`` ⇒ the whole-``feeds/``-prefix structural exclusion applies.
    """

    name: str
    resolved_root: Path
    allowed_parts: tuple[tuple[str, ...], ...]
    foreign_parts: tuple[tuple[str, ...], ...]
    primary: bool


def primary_write_domain(
    lake_root: Path, *, registered_feed_subtrees: tuple[str, ...] = ()
) -> WriteDomain:
    """Build the PRIMARY (engine-feeding) write domain over *lake_root*.

    The primary domain = the resolved lake root MINUS the ENTIRE ``feeds/``
    path-component prefix — STRUCTURALLY, registration-INDEPENDENT (the
    ADR-0021 fold-#4 NG3-EDGE law: a primary-profile write under
    ``feeds/<anything>/`` refuses whether or not that subtree is registered).
    *registered_feed_subtrees* (e.g. ``("feeds/<vendor>",)``) is carried as
    the explicit foreign belt for refusal-message clarity; it never WIDENS or
    NARROWS the structural exclusion.

    The ONE ``lake_root.resolve()`` happens here — registration time, never
    per assert (a guarded resolve: an unresolvable root is a loud
    registration failure).
    """
    return WriteDomain(
        name=PRIMARY_DOMAIN_NAME,
        resolved_root=_resolve_registration_root(lake_root),
        allowed_parts=(),
        foreign_parts=tuple(_clean_relative_parts(s) for s in registered_feed_subtrees),
        primary=True,
    )


def feeds_write_domain(
    lake_root: Path, subtree: str, *, foreign_feed_subtrees: tuple[str, ...] = ()
) -> WriteDomain:
    """Build a FEED-vendor write domain: exactly its own ``feeds/<vendor>/`` subtree.

    *subtree* must sit under the ``feeds/`` component (ADR-0021 Decision 1:
    every second-vendor byte nests there — a feed profile claiming a subtree
    outside ``feeds/`` is a mis-registration, refused loudly). The domain
    allows ONLY that subtree; a dest under the primary trees OR under a
    FOREIGN ``feeds/<other>/`` subtree refuses (the allowed-set form makes
    the foreign-feeds refusal structural — not-under-own-subtree — and
    *foreign_feed_subtrees* is the explicit belt the refusal message names).
    """
    parts = _clean_relative_parts(subtree)
    if parts[0] != FEEDS_COMPONENT or len(parts) < _MIN_FEED_SUBTREE_PARTS:
        raise WriteDomainError(
            f"feed write-domain subtree {subtree!r} must sit under "
            f"'{FEEDS_COMPONENT}/<vendor>' (ADR-0021 Decision 1 — the feed-root "
            "nesting law; the primary trees are never a feed domain)"
        )
    return WriteDomain(
        name=subtree,
        resolved_root=_resolve_registration_root(lake_root),
        allowed_parts=(parts,),
        foreign_parts=tuple(
            foreign for s in foreign_feed_subtrees if (foreign := _clean_relative_parts(s)) != parts
        ),
        primary=False,
    )


def _resolve_registration_root(lake_root: Path) -> Path:
    """Resolve the lake root ONCE at registration — guarded, fail-loud."""
    try:
        return lake_root.resolve()
    except (OSError, RuntimeError) as exc:
        # Symlink loop / unresolvable root: RuntimeError on Python ≤3.12,
        # OSError (ELOOP) on 3.13+ — either way a loud registration refusal
        # (the R5-fold guarded-resolve idiom).
        raise WriteDomainError(
            f"cannot resolve lake root {lake_root} for write-domain registration: {exc}"
        ) from exc


def _is_under(rel_parts: tuple[str, ...], subtree_parts: tuple[str, ...]) -> bool:
    """``True`` iff *rel_parts* sits at/under the *subtree_parts* prefix — pure, O(len)."""
    return rel_parts[: len(subtree_parts)] == subtree_parts


def _assert_feeds_component_not_swapped(
    dest: Path, resolved: Path, *, resolved_root: Path, domain_name: str
) -> None:
    """The round-6 IN-LAKE component-swap rail (the assurance NG4E-2 MAJOR fold).

    The resolved-path judgment in :func:`assert_write_domain` FAILS OPEN when
    ``<root>/feeds`` itself is a symlink: the ``feeds`` token resolves away,
    so a PRIMARY-profile dest addressed under ``feeds/<vendor>/`` passes the
    literal-prefix exclusion while physically landing in the feed vendor's
    real bytes (empirically reproduced — ``(root/'feeds').symlink_to(
    root/'other')``), and the feeds profile simultaneously mis-refuses its
    own tree. Ported from the DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 round-6 rail
    (:meth:`~brutex.data.option_parquet_writer.OptionParquetWriter._assert_options_containment`):
    probe the root's ``feeds`` component with ``is_symlink()`` — a REAL
    ``feeds`` dir (or none at all) is a no-op — and refuse, NAMING the
    symlinked component, any dest whose RESOLVED path sits under the swapped
    component's resolved target (fail-closed BOTH directions: the primary
    fail-open repro AND the feeds profile's swapped-out own tree). Ancestors
    ABOVE the root are deliberately unprobed — a symlinked lake ROOT (the
    legitimate mount shape) keeps passing; only the in-lake ``feeds``
    component gets the rail. A dest away from the swap target (a bars-tree
    write while ``feeds`` is swapped) still passes. The probe + the target
    resolve are GUARDED (the R5 idiom) — an unprobeable / unresolvable
    ``feeds`` component is the same typed fail-closed refusal, never a bare
    ``RuntimeError``. Cost: one ``lstat`` per asserted dest (+ one guarded
    resolve ONLY when the component is actually a symlink). Shared by
    :func:`assert_write_domain` (per-domain) AND the profile-free
    :func:`assert_profile_free_dest` masquerade pin — *resolved_root* /
    *domain_name* are passed explicitly so the pin needs no
    :class:`WriteDomain` (DEC-WRITE-DOMAIN-CALLER-BINDING-001).
    """
    feeds_component = resolved_root / FEEDS_COMPONENT
    try:
        if not feeds_component.is_symlink():
            return
        swapped_target = feeds_component.resolve()
    except (OSError, RuntimeError) as exc:
        raise WriteDomainError(
            f"cannot probe the '{FEEDS_COMPONENT}/' component under "
            f"{resolved_root} for the {domain_name!r} write-domain check: "
            f"{exc} — refusing to touch {dest}"
        ) from exc
    if resolved.is_relative_to(swapped_target):
        raise WriteDomainError(
            f"the '{FEEDS_COMPONENT}/' component under the lake root is a SYMLINK "
            f"(-> {swapped_target}) — the feed tree has been swapped out, and write "
            f"dest {dest} (resolved {resolved}) sits under its target; refused for "
            f"the {domain_name!r} domain (the DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 "
            "round-6 in-lake swap rail; ADR-0021 Pin D1-1)"
        )


def assert_write_domain(dest: Path, domain: WriteDomain) -> None:
    """The Pin D1-1 runtime pin: refuse a *dest* outside *domain* — typed, loud.

    The dest side is FULLY resolved (guarded — an unresolvable / symlink-loop
    dest is the same typed refusal); the domain side was resolved once at
    registration with its subtree components literal (the R6 idiom — see the
    module docstring). Containment is judged on the POST-realpath path
    RELATIVE to the resolved root. Refusal cases, each a
    :class:`WriteDomainError` naming both sides:

    * dest unresolvable (symlink loop / OS error);
    * ANY domain × a dest under a SWAPPED ``<root>/feeds`` symlink component
      (the round-6 in-lake swap rail, :func:`_assert_feeds_component_not_swapped`
      — the assurance NG4E-2 MAJOR fold: without it the primary exclusion
      FAILS OPEN when the ``feeds`` token resolves away; the refusal names
      the symlinked component, both directions);
    * dest outside the lake root entirely;
    * PRIMARY domain × ANY ``feeds/``-prefixed dest — registered or NOT (the
      fold-#4 NG3-EDGE law; the nested-root false-pass class);
    * FEEDS domain × a dest outside its OWN subtree — the primary trees AND
      any foreign ``feeds/<other>/`` subtree alike;
    * any domain × a dest under an explicitly-registered FOREIGN subtree
      (the belt arm — structurally subsumed by the two rules above for
      today's shapes, kept for future non-``feeds/`` domains).

    O(1) in lake size: one resolve + one ``lstat`` swap-rail probe +
    O(subtrees) tuple-prefix compares.
    NEVER keep-first, never a log-and-continue — the writer must not touch
    the dest (ADR-0021 Pin D1-1).

    Honest limit: every component compare here is byte/case-SENSITIVE — a
    differently-cased ``Feeds/`` dest is NOT the feeds subtree to this check
    even on a case-insensitive filesystem where it aliases the same
    directory (see the module docstring's case-sensitivity section; the
    production writers only ever mint the lowercase :data:`FEEDS_COMPONENT`,
    and the behavior is test-pinned so a casefold is never a silent edit).
    """
    try:
        resolved = dest.resolve()
    except (OSError, RuntimeError) as exc:
        raise WriteDomainError(
            f"cannot resolve write dest {dest} for the {domain.name!r} "
            f"write-domain check: {exc} — refusing to touch it"
        ) from exc
    # The round-6 in-lake swap rail runs FIRST (before the relative_to
    # judgment) so a swapped `feeds` component gets the NAMED refusal on
    # both profiles — including the out-of-lake-target shape, which would
    # otherwise fall into the generic outside-the-root arm.
    _assert_feeds_component_not_swapped(
        dest, resolved, resolved_root=domain.resolved_root, domain_name=domain.name
    )
    try:
        rel_parts = resolved.relative_to(domain.resolved_root).parts
    except ValueError as exc:
        raise WriteDomainError(
            f"write dest {dest} (resolved {resolved}) falls OUTSIDE the lake root "
            f"{domain.resolved_root} — refused for the {domain.name!r} domain"
        ) from exc
    if domain.primary:
        if rel_parts[:1] == (FEEDS_COMPONENT,):
            # The structural whole-prefix exclusion (fold #4, NG3-EDGE): a
            # primary-profile write under feeds/<ANYTHING>/ refuses whether or
            # not the subtree is registered — the registered-complement form
            # would false-pass an unregistered feeds/<other>/ dest.
            raise WriteDomainError(
                f"write dest {dest} (resolved {resolved}) sits under the "
                f"'{FEEDS_COMPONENT}/' feed prefix — the PRIMARY write domain "
                "excludes the entire feed tree structurally "
                "(registration-independent; ADR-0021 Pin D1-1 / NG3-EDGE)"
            )
    elif not any(_is_under(rel_parts, allowed) for allowed in domain.allowed_parts):
        raise WriteDomainError(
            f"write dest {dest} (resolved {resolved}) falls outside the "
            f"{domain.name!r} feed write domain (allowed subtree(s): "
            f"{', '.join('/'.join(p) for p in domain.allowed_parts)}) — "
            "cross-domain writes are refused, never keep-first-merged "
            "(ADR-0021 Pin D1-1)"
        )
    for foreign in domain.foreign_parts:
        if _is_under(rel_parts, foreign):
            raise WriteDomainError(
                f"write dest {dest} (resolved {resolved}) sits inside the FOREIGN "
                f"registered subtree '{'/'.join(foreign)}' — cross-domain writes "
                f"are refused for the {domain.name!r} domain (ADR-0021 Pin D1-1)"
            )


def resolve_profile_free_root(lake_root: Path) -> Path:
    """Resolve *lake_root* ONCE for repeated masquerade-pin asserts — fail-loud.

    The profile-free twin of the registration-time resolve (the same guarded
    idiom): a caller holding a profile-FREE writer resolves its lake root
    once (cache it) and passes the result to every
    :func:`assert_profile_free_dest` call — never a per-write root resolve
    (DEC-WRITE-DOMAIN-CALLER-BINDING-001). An unresolvable root (symlink
    loop / OS error) is the typed :class:`WriteDomainError`.
    """
    return _resolve_registration_root(lake_root)


def assert_profile_free_dest(dest: Path, resolved_root: Path) -> None:
    """The MASQUERADE pin: a PROFILE-FREE writer must never write under ``feeds/``.

    DEC-WRITE-DOMAIN-CALLER-BINDING-001 — the close of NG4E-1's
    domain-less-writer arm (the vendor-profile-registry deferral register's
    item (b) — PR #1570's on-branch ledger #1833): a writer
    constructed with NO :class:`WriteDomain` refuses, typed, any dest whose
    path RELATIVE to *resolved_root* (the writer's own lake root, resolved
    once via :func:`resolve_profile_free_root`) enters the ``feeds/``
    subtree — a domain-less writer silently landing bytes in a feed vendor's
    tree is the MASQUERADE class (it would wear the feed vendor's provenance
    without its profile). Composes the round-6 in-lake swap rail
    (:func:`_assert_feeds_component_not_swapped`), so a
    ``bars/``-addressed dest that RESOLVES under a swapped
    ``<root>/feeds`` symlink target refuses too.

    Deliberately NARROW (the lane-assignment law in the module docstring —
    every maintenance lane stays byte-identical today):

    * a dest OUTSIDE *resolved_root* entirely is NOT the masquerade class —
      it passes this pin unjudged (legacy behavior preserved; out-of-root
      containment belongs to the per-writer rails, e.g. the option writer's
      heal containment, never to this feeds-only pin);
    * every component compare is byte/case-SENSITIVE (the module docstring's
      case-sensitivity section — production writers mint only the lowercase
      :data:`FEEDS_COMPONENT`);
    * the LAKE-RELATIVE residual: a profile-free writer whose OWN root sits
      inside a feed subtree is invisible here (see the module docstring's
      honest-residual paragraph — the profile/slot layer owns that
      construction shape).

    O(1) in lake size: one guarded dest resolve + one ``lstat`` swap-rail
    probe + one tuple-prefix compare.
    """
    try:
        resolved = dest.resolve()
    except (OSError, RuntimeError) as exc:
        raise WriteDomainError(
            f"cannot resolve write dest {dest} for the {PROFILE_FREE_NAME!r} "
            f"masquerade check: {exc} — refusing to touch it"
        ) from exc
    _assert_feeds_component_not_swapped(
        dest, resolved, resolved_root=resolved_root, domain_name=PROFILE_FREE_NAME
    )
    try:
        rel_parts = resolved.relative_to(resolved_root).parts
    except ValueError:
        # Out-of-root is NOT the masquerade class — this pin judges ONLY the
        # feeds/ subtree of the writer's own root (deliberately narrow; see
        # the docstring). A profile-free writer's other dests keep today's
        # behavior byte-identical.
        return
    if rel_parts[:1] == (FEEDS_COMPONENT,):
        raise WriteDomainError(
            f"write dest {dest} (resolved {resolved}) sits under the "
            f"'{FEEDS_COMPONENT}/' feed prefix but the writer carries NO write "
            "domain — the profile-free MASQUERADE class: a domain-less writer "
            "must never silently write into a feed vendor's tree "
            "(DEC-WRITE-DOMAIN-CALLER-BINDING-001; ADR-0021 Pin D1-1 / NG4E-1)"
        )


__all__ = [
    "FEEDS_COMPONENT",
    "PRIMARY_DOMAIN_NAME",
    "PROFILE_FREE_NAME",
    "WriteDomain",
    "WriteDomainError",
    "assert_profile_free_dest",
    "assert_write_domain",
    "feeds_write_domain",
    "primary_write_domain",
    "resolve_profile_free_root",
]
