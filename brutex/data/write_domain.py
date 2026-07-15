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

## Path resolution (the DEC-LAKE-HEAL-OPTIONS-FOREIGN-001 R6 precedent)

The containment test mirrors
:meth:`~brutex.data.option_parquet_writer.OptionParquetWriter._assert_options_containment`
exactly: the lake ROOT is resolved ONCE at registration and the domain's
subtree components are appended LITERALLY — the final path components
(``feeds``, the vendor dir, ``bars``, …) are deliberately NOT
symlink-resolved, because resolving them would re-anchor the containment
root to wherever a swapped-in symlink points (re-opening the hole); the DEST
side IS fully resolved (``dest.resolve()``, guarded — a symlink-loop /
unresolvable dest is the same typed refusal, the R5 fold), so an
escape-by-symlink dest is caught while a lake ROOT reached via a symlink (the
legitimate mount shape) still passes — BOTH sides resolve the root
consistently (test-pinned, the mount-shape fixture).

## Scoping (the coordinator-relayed WS-B obligation NG4E-1)

The refusal binds **PROFILE-BOUND vendor-chain writers ONLY** — the staged
pull chains a :class:`WriteDomain`-carrying vendor profile constructs.
Maintenance / restore lanes are **profile-FREE by design** and never consult
this module: the nightly restore (:mod:`brutex.nightly.restore` — its own
verify-everything-then-write law) and the off-box restic restore
(DEC-BACKUP-OFFBOX-001 — whole-root snapshots) legitimately write across the
whole lake root, feed subtrees included; wrapping them in a per-vendor
domain would break whole-lake disaster recovery for zero isolation gain
(they never race a vendor chain — the restore runbooks stop the pulls
first).

## Restore ordering across dual window ledgers (the coordinator-relayed WS-B
## obligation NG4E-2 — PROPOSED + DOCUMENTED here, flagged for assurance
## review at the WS-B review)

When a second vendor's window ledger lands under its feed subtree (ADR-0021
Pin D1-2), a whole-lake restore involves TWO ledgers. Proposed ordering law,
generalizing the shipped single-ledger law ("`ledger.parquet` strictly LAST"
— DEC-SWEEP-NIGHTLY-AUTOMATION-001's restore-order rationale: the ledger is
the pull's skip authority, so it must never claim coverage of bars that were
not yet restored): **each domain's window ledger restores strictly LAST
WITHIN its own domain's files, and the PRIMARY ledger restores last
overall** — i.e. restore primary bars/options → feed-subtree bars → the feed
ledger → the primary ledger. Rationale: within a domain the ledger-last law
is the existing correctness argument verbatim (per-domain, since each ledger
only describes its own domain's bars); across domains the primary ledger is
the one surface the PRIMARY (engine-feeding) chain resumes from, so it lands
last — a crash mid-restore then leaves the primary chain conservatively
re-verifying (idempotent keep-first re-pull) rather than skipping. This is a
PROPOSAL recorded for the assurance review — no restore lane consumes it in
WS-B (the wiring is WS-C+, alongside Pin D1-2's backup-allowlist add).

## WS-B envelope (honest limits)

This module ships the PRIMITIVE + its negative-pin battery ONLY. NO writer
consults it yet — wiring :func:`assert_write_domain` into the staged
vendor-chain writers (and the Pin D1-3 read-side twin on the Greeks
spot-join) is WS-C, alongside the ``VendorPullProfile`` registry that
constructs the domains. Cost: one guarded ``Path.resolve`` + O(subtrees)
tuple-prefix compares per asserted dest — O(1) in lake size.
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
    if parts[0] != FEEDS_COMPONENT or len(parts) < 2:
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


def assert_write_domain(dest: Path, domain: WriteDomain) -> None:
    """The Pin D1-1 runtime pin: refuse a *dest* outside *domain* — typed, loud.

    The dest side is FULLY resolved (guarded — an unresolvable / symlink-loop
    dest is the same typed refusal); the domain side was resolved once at
    registration with its subtree components literal (the R6 idiom — see the
    module docstring). Containment is judged on the POST-realpath path
    RELATIVE to the resolved root. Refusal cases, each a
    :class:`WriteDomainError` naming both sides:

    * dest unresolvable (symlink loop / OS error);
    * dest outside the lake root entirely;
    * PRIMARY domain × ANY ``feeds/``-prefixed dest — registered or NOT (the
      fold-#4 NG3-EDGE law; the nested-root false-pass class);
    * FEEDS domain × a dest outside its OWN subtree — the primary trees AND
      any foreign ``feeds/<other>/`` subtree alike;
    * any domain × a dest under an explicitly-registered FOREIGN subtree
      (the belt arm — structurally subsumed by the two rules above for
      today's shapes, kept for future non-``feeds/`` domains).

    O(1) in lake size: one resolve + O(subtrees) tuple-prefix compares.
    NEVER keep-first, never a log-and-continue — the writer must not touch
    the dest (ADR-0021 Pin D1-1).
    """
    try:
        resolved = dest.resolve()
    except (OSError, RuntimeError) as exc:
        raise WriteDomainError(
            f"cannot resolve write dest {dest} for the {domain.name!r} "
            f"write-domain check: {exc} — refusing to touch it"
        ) from exc
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


__all__ = [
    "FEEDS_COMPONENT",
    "PRIMARY_DOMAIN_NAME",
    "WriteDomain",
    "WriteDomainError",
    "assert_write_domain",
    "feeds_write_domain",
    "primary_write_domain",
]
