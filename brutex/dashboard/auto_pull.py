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
