"""Decision seam for the on-run expiries/contracts auto-pull.

Pure, dependency-light gate powering the credentials-gated, non-blocking
background universe pull wired into ``cmd_dashboard`` (DEC-AUTO-UNIVERSE-001).
Per CLAUDE.md §11 (COMMON) the *decision* of whether to launch the live
expiries/contracts pull lives here in exactly ONE place so it is 100%
unit-testable offline, while the actual live vendor REST client + background-thread
glue in :mod:`brutex.cli` (coverage-excluded) stays a thin wrapper.

## The gate (locked design)

The keyless public instrument master loads on startup with NO vendor
credentials (DEC-INDEX-PARSE-FIX-001, CLAUDE.md §6 — the instrument
master URL needs no auth). Expiries and contracts, by contrast, are
fetched through the authenticated vendor REST client (CLAUDE.md §6 — the
``get_expiries`` / ``get_contracts`` methods require a session), so the
auto-pull must be **gated on credentials being present**. It must also
have something to pull *for*: the per-underlying expiry/contract fan-out
iterates the loaded indices, so the instrument master must already be
populated. Both conditions are required.

DEC-RUST-PULL-STATE-001 (flip phase): the reload predicate's decision plane
IS the ``brutex_pull_state`` Rust kernel
(:func:`brutex.pull_state_rust.rust_should_reload_master`); the former
byte-identical Python oracle body is DELETED. A missing/stale/raising wheel —
or the removed ``BRUTEX_PULL_STATE_RUST`` knob still set — raises the typed
error out of this predicate, FAIL-CLOSED (the boot-time reload decision stops
loudly, never silently guesses).
"""

from __future__ import annotations

from brutex import pull_state_rust as _pull_state_rust


def should_reload_instrument_master(
    *,
    index_count: int,
    stock_count: int,
    legs_present: bool = False,
    stale_economics: bool = False,
    stale_eligibility: bool = False,
) -> bool:
    """Return whether the keyless instrument master should be (re)loaded on run.

    Reloads when the lake is in any of five known-bad states:

    * **empty** — nothing loaded yet (``index_count == stock_count == 0``);
    * **indices missing** — holds stocks yet **zero indices**, the fingerprint
      of a pre-DEC-INDEX-PARSE-FIX-001 build that quarantined every index row;
    * **non-lean** — still holds FUT/CE/PE legs (``legs_present``), the
      fingerprint of a pre-DEC-INSTRUMENT-UNIVERSE-FAST-001 build that stored
      the full ~101k-row master. The lean refresh does a full-snapshot replace
      (DEC-LEAN-REINGEST-FIX-001), so reloading purges the stale dump down to
      the ~236 universe underlyings instead of serving over-counted data;
    * **stale economics** — holds stocks at the master's placeholder
      ``lot_size = 1`` (``stale_economics``; the probe keys on ``lot_size`` —
      every real NSE F&O stock lot is well above 1), the
      fingerprint of a lake loaded BEFORE the leg-derived back-fill landed
      (DEC-UNDERLYING-ECONOMICS-001). Without this trigger the auto-heal kept
      serving ``lot_size = 1`` forever, because the placeholder economics did
      not change the index/stock counts and the lean lake carries no legs
      (DEC-STALE-ECONOMICS-HEAL-001). A clean re-load enriches every stock
      from its derivative legs;
    * **stale eligibility** — the OPT/REF flags were written by the retired
      id-collision tagger (``stale_eligibility``; the probe keys on a
      brute-force index wrongly ``reference_only`` or INDIA VIX wrongly
      option-pullable — DEC-ELIGIBILITY-AUTOHEAL-001). A re-load re-tags every
      row with the precise OPTIDX-only arm (DEC-OPTIONS-ELIGIBLE-PRECISE-001),
      so an upgraded build heals itself instead of waiting for the operator to
      remember the manual "Load instruments" click.

    Re-loading is safe + idempotent: the lean refresh replaces the whole
    snapshot deterministically, and a fully-enriched lake clears
    ``stale_economics`` so the reload fires at most once.

    Parameters
    ----------
    index_count
        Indices currently in the lake snapshot.
    stock_count
        F&O-eligible stocks currently in the lake snapshot.
    legs_present
        ``True`` iff the lake still contains any FUT/CE/PE leg (a pre-lean
        lake). Cheaply probed via
        :meth:`~brutex.data.instrument_store.InstrumentStore.has_derivative_legs`.
    stale_economics
        ``True`` iff the lake holds an F&O-eligible equity with placeholder
        (un-enriched) ``lot_size``. Cheaply probed via
        :meth:`~brutex.data.instrument_store.InstrumentStore.has_placeholder_stock_economics`.
    stale_eligibility
        ``True`` iff the latest snapshot carries pre-precise OPT/REF flags.
        Cheaply probed via
        :meth:`~brutex.data.instrument_store.InstrumentStore.has_stale_option_eligibility`.

    Returns
    -------
    bool
        ``True`` iff the lake is empty, missing indices, non-lean, carries
        stale (un-enriched) stock economics, OR carries stale OPT/REF
        eligibility flags.

    Raises
    ------
    brutex.pull_state_rust.PullStateKernelError
        Wheel absent/stale, kernel raise, a poisoned (non-bool) result, or a
        count outside the kernel's i64 domain — fail-closed, no oracle exists
        (DEC-RUST-PULL-STATE-001; the pre-flip silent Python arm for
        out-of-i64 counts was deleted with the oracle).
    brutex.pull_state_rust.PullStateFlagRemovedError
        The removed ``BRUTEX_PULL_STATE_RUST`` knob is still set.
    """
    # --- auto_pull (Wave 3G, flip phase — DEC-RUST-PULL-STATE-001): the ONE
    # predicate IS the Rust kernel (five facts in, one bool out); the former
    # byte-identical Python oracle body is DELETED.
    return _pull_state_rust.rust_should_reload_master(
        index_count=index_count,
        stock_count=stock_count,
        legs_present=legs_present,
        stale_economics=stale_economics,
        stale_eligibility=stale_eligibility,
    )


# NOTE: ``should_auto_pull_universe`` was removed in DEC-UNIVERSE-PULL-PHASED-001
# (Stage 2) when the startup auto-pull was retired in favour of the two manual
# /universe buttons ("Fetch all expiries" / "Fetch all contracts"). The pull is
# now operator-triggered, so the auto-pull gate has no caller; it was deleted
# rather than left as tested-but-unreachable code (Golden Rule #10 / §12).
# ``should_reload_instrument_master`` above is unaffected — the keyless master
# still auto-loads on run.
