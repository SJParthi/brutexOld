"""Decision seam for the on-run expiries/contracts auto-pull.

Pure, dependency-light gate powering the credentials-gated, non-blocking
background universe pull wired into ``cmd_dashboard`` (DEC-AUTO-UNIVERSE-001).
Per CLAUDE.md §11 (COMMON) the *decision* of whether to launch the live
expiries/contracts pull lives here in exactly ONE place so it is 100%
unit-testable offline, while the actual live Groww SDK + background-thread
glue in :mod:`brutex.cli` (coverage-excluded) stays a thin wrapper.

## The gate (locked design)

The keyless public instrument master loads on startup with NO Groww
credentials (DEC-INDEX-PARSE-FIX-001, CLAUDE.md §6 — the instrument
master URL needs no auth). Expiries and contracts, by contrast, are
fetched through the authenticated Groww SDK (CLAUDE.md §6 — the
``get_expiries`` / ``get_contracts`` methods require a session), so the
auto-pull must be **gated on credentials being present**. It must also
have something to pull *for*: the per-underlying expiry/contract fan-out
iterates the loaded indices, so the instrument master must already be
populated. Both conditions are required.
"""

from __future__ import annotations


def should_reload_instrument_master(*, index_count: int, stock_count: int) -> bool:
    """Return whether the keyless instrument master should be (re)loaded on run.

    The master is re-fetched on startup not only when the lake is wholly
    empty, but also when it is **stale** — specifically when it holds
    stocks yet **zero indices**. That exact state is the fingerprint of a
    lake populated by a pre-DEC-INDEX-PARSE-FIX-001 build (which quarantined
    all 30 index rows; CLAUDE.md §6), so a plain "non-empty → skip" check
    would pin the operator to stale, index-less data forever. Re-loading is
    safe and idempotent: the master upsert dedups on ``vendor_symbol``
    (DEC-INDEX-PARSE-FIX-001), so a redundant reload is a no-op write.

    Parameters
    ----------
    index_count
        Indices currently in the lake snapshot.
    stock_count
        F&O-eligible stocks currently in the lake snapshot.

    Returns
    -------
    bool
        ``True`` iff the lake is missing indices (``index_count == 0``) —
        which covers both a wholly empty lake (``stock_count`` also 0) and
        a stale lake that an old build populated with stocks but no indices.
        ``stock_count`` is accepted for an explicit, self-documenting
        signature even though the index count alone decides the gate.
    """
    lake_is_empty = index_count == 0 and stock_count == 0
    indices_missing = index_count == 0
    return lake_is_empty or indices_missing


def should_auto_pull_universe(*, credentials_present: bool, instrument_total: int) -> bool:
    """Return whether the background expiries/contracts pull should run.

    The auto-pull is launched only when **both** preconditions hold:

    * ``credentials_present`` — Groww auth is configured (env vars or the
      saved 0600 credentials file), since expiries/contracts go through
      the authenticated SDK (CLAUDE.md §6). The keyless instrument
      master does not satisfy this.
    * ``instrument_total > 0`` — the instrument master is already loaded,
      so there are underlyings to fan out expiries/contracts for.

    Parameters
    ----------
    credentials_present
        ``True`` iff Groww credentials resolve from the environment or
        the saved credentials file.
    instrument_total
        Total instruments currently in the lake snapshot.

    Returns
    -------
    bool
        ``True`` iff the background universe pull should be launched.
    """
    return credentials_present and instrument_total > 0
