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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from brutex.config.feed_scope import DEFAULT_FEED, FeedScope, resolve_feed_scope
from brutex.models import Exchange, InstrumentType

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (no runtime edge)
    from collections.abc import Iterable


def store_scope_mismatch(
    *,
    scope: FeedScope,
    index_rows: Iterable[tuple[Exchange, str]],
    eq_row_count: int,
) -> bool:
    """The DEC-UNIVERSE-INDEX-ONLY-001 store↔scope mismatch probe (design §5.1).

    TWO symmetric arms, keyed on the LOADER's OWN feed scope (groww) — NEVER
    the read union (a wide NON-groww binding — ``gdfl=full`` — must never arm
    a reload that runs under groww's narrow scope, still finds zero EQ rows,
    and loops every boot):

    * **narrow arm** — a BOUNDED scope over a store holding any EQ row (while
      ``admits_stocks()`` is False) or any IDX row OUTSIDE the admitted set:
      a stale ~750-row store fires ONE reload; the full-snapshot
      replace-and-prune shrinks it to the admitted rows; the arm reads False.
      No loop.
    * **widen arm** — ``admits_stocks()`` over a store holding ZERO EQ rows
      (an under-populated-for-scope store, e.g. a pruned 4-row store after
      ``BRUTEX_FEED_SCOPE=full`` lands persistently): fires ONE reload; a
      SUCCESSFUL full-scope load writes the EQ rows, so the arm reads False.

    Loop-freedom (stated, not assumed — the #696/DEC-STALE-ECONOMICS-HEAL-001
    class): (a) the arms key on the loader's own feed scope; (b) a FAILED
    master fetch keeps the caller's best-effort ``autoload_skipped`` posture
    (the probe is consulted once per BOOT — no intra-boot loop); (c) both
    no-loop directions are test-pinned; (d) per-process ENV DIVERGENCE is the
    third, DESTRUCTIVE direction: every narrow-env boot over a
    deliberately-wide-loaded store fires the narrow arm and reload-PRUNES the
    wide rows (+ one vendor fetch per such boot) — governed by the §5.6
    persistent-env mandate, made log-visible by the acting ``scope=<token>``
    in the reload log, pinned by the §6#11 divergence pin.

    Parameters
    ----------
    scope
        The LOADER's own (acting-feed) scope.
    index_rows
        ``(exch_id, symbol_name)`` of every stored IDX row.
    eq_row_count
        Count of stored EQ rows (raw — classification-independent).
    """
    if scope.bounded():
        if eq_row_count > 0 and not scope.admits_stocks():
            return True
        return any(not scope.admits_underlying(exch, sym) for exch, sym in index_rows)
    # Unbounded (`full`): the widen arm — a store pruned narrow under a prior
    # index4 boot holds zero EQ rows; one reload re-populates the wide set.
    return scope.admits_stocks() and eq_row_count == 0


def evaluate_master_reload_trigger(
    store: Any,
    *,
    scope: FeedScope | None = None,
) -> tuple[bool, dict[str, int]]:
    """Assemble EVERY instrument-master reload trigger off *store* (covered helper).

    DEC-UNIVERSE-INDEX-ONLY-001 (design §5.1): the trigger assembly is
    extracted OUT of ``cli._autoload_instrument_master``'s pragma-no-cover
    daemon body into this covered, spy-testable helper (the call-site kwargs
    carry their own source pin — the #1645 idiom), so a dropped kwarg can
    never ship invisibly. Reads the store's candidate rows ONCE, folds the
    lean-correct counts (:func:`~brutex.dashboard.universe_query.universe_summary`)
    plus the raw-row scope-mismatch probe (:func:`store_scope_mismatch`, keyed
    on the LOADER's own feed *scope* — ``None`` → the default acting feed's),
    and returns ``(should_reload, summary)``.
    """
    # Local import: auto_pull stays a dependency-light decision seam; the
    # summary import pulls the classifier chain only when this store-reading
    # helper actually runs (the cli daemon path).
    from brutex.dashboard.universe_query import universe_summary

    if scope is None:
        scope = resolve_feed_scope(DEFAULT_FEED)
    candidates = list(store.list_index_equity_instruments())
    summary = universe_summary(candidates, fno_eligible=store.fno_eligible_underlyings())
    index_rows = [
        (inst.exch_id, inst.symbol_name)
        for inst in candidates
        if inst.instrument == InstrumentType.IDX
    ]
    eq_row_count = sum(1 for inst in candidates if inst.instrument == InstrumentType.EQ)
    should = should_reload_instrument_master(
        index_count=summary["index_count"],
        stock_count=summary["stock_count"],
        legs_present=store.has_derivative_legs(),
        stale_economics=store.has_placeholder_stock_economics(),
        stale_eligibility=store.has_stale_option_eligibility(),
        scope_mismatch=store_scope_mismatch(
            scope=scope, index_rows=index_rows, eq_row_count=eq_row_count
        ),
    )
    return should, summary


def should_reload_instrument_master(
    *,
    index_count: int,
    stock_count: int,
    legs_present: bool = False,
    stale_economics: bool = False,
    stale_eligibility: bool = False,
    scope_mismatch: bool = False,
) -> bool:
    """Return whether the keyless instrument master should be (re)loaded on run.

    Reloads when the lake is in any of six known-bad states:

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
    scope_mismatch
        ``True`` iff the stored rows mismatch the LOADER's own universe scope
        in EITHER direction (:func:`store_scope_mismatch` —
        DEC-UNIVERSE-INDEX-ONLY-001 §5.1: a stale ~750-row store under
        ``index4`` fires ONE narrowing reload; a pruned 4-row store under a
        persistent ``full`` env fires ONE widening reload; both arms are
        loop-free by construction, see that probe's docstring).

    Returns
    -------
    bool
        ``True`` iff the lake is empty, missing indices, non-lean, carries
        stale (un-enriched) stock economics, carries stale OPT/REF
        eligibility flags, OR mismatches the acting universe scope.
    """
    lake_is_empty = index_count == 0 and stock_count == 0
    indices_missing = index_count == 0
    return (
        lake_is_empty
        or indices_missing
        or legs_present
        or stale_economics
        or stale_eligibility
        or scope_mismatch
    )


# NOTE: ``should_auto_pull_universe`` was removed in DEC-UNIVERSE-PULL-PHASED-001
# (Stage 2) when the startup auto-pull was retired in favour of the two manual
# /universe buttons ("Fetch all expiries" / "Fetch all contracts"). The pull is
# now operator-triggered, so the auto-pull gate has no caller; it was deleted
# rather than left as tested-but-unreachable code (Golden Rule #10 / §12).
# ``should_reload_instrument_master`` above is unaffected — the keyless master
# still auto-loads on run.
