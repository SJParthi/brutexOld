"""Keyless instrument-master load for the ``/universe`` dashboard (Groww).

The ``/universe`` page is empty until SOMEONE has populated the instrument
master — the list of NSE/BSE indices + F&O-eligible single stocks. That master
is a **public CSV** (the Groww instrument master,
``https://growwapi-assets.groww.in/instruments/instrument.csv``) that needs
**NO Groww API token** — only candles / options need authentication, and those
are a separate, later concern (DEC-VENDOR-PIVOT-001).

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): this module
was repointed from the retired-vendor 31-column detailed scrip master to the
Groww 21-column instrument master — the J2-equivalent the dashboard never got.
The Groww CSV schema differs, so the parse goes through the Groww parser
(:func:`~brutex.adapters.groww.instrument_loader.load_index` →
:class:`~brutex.adapters.groww.instruments.InstrumentIndex`), NOT the retired one.

This module is the thin, fully-testable seam that:

1. downloads + parses the Groww instrument master into an in-memory
   :class:`~brutex.adapters.groww.instruments.InstrumentIndex` (the SSRF-guarded,
   **no-auth** public-CSV path), then
2. derives the F&O-eligible equity set + the per-underlying tradable lot/freeze
   from the parsed derivative legs and **full-snapshot upserts the LEAN universe**
   (every admitted IDX + every F&O-eligible EQ + every admitted REFERENCE_ONLY NTM
   constituent, each stamped with its F&O lot + freeze from its legs per
   DEC-GROWW-LEG-ECONOMICS-001; the ~tens-of-thousands of FUT / CE / PE legs are
   then DISCARDED per DEC-INSTRUMENT-UNIVERSE-FAST-001) into the vendor-neutral
   :class:`~brutex.data.instrument_store.InstrumentStore`, then
3. reads the snapshot back and summarises it with the COMMON
   :func:`brutex.dashboard.universe_query.universe_summary` classifier
   (no re-derivation of the index-vs-stock split).

There is deliberately **no** auth reference anywhere in this path — populating
the LIST is keyless by design.

The live HTTP fetch
(:func:`~brutex.adapters.groww.instrument_loader.urllib_http_get`) is
``# pragma: no cover`` because CI is network-blocked; it is reached on the
operator's Mac. Tests inject a fake ``http_get`` returning a small canned Groww
CSV against a REAL temp
:class:`~brutex.data.instrument_store.InstrumentStore`, so every non-pragma line
+ branch in this module is covered offline.

## NTM (NIFTY Total Market) enrichment — re-enabled on Groww

The retired-vendor loader auto-admitted NIFTY Total Market's ~750 constituents
(DEC-NIFTY-TOTAL-MARKET-LOAD-001) as REFERENCE_ONLY lake rows. This module
re-enables that on Groww (DEC-NTM-GROWW-REPOINT-001) by mirroring the SEMANTICS,
not the retired vendor calls:

* the constituent SOURCE is already vendor-agnostic — NSE's per-index CSV from
  ``niftyindices.com`` (:func:`brutex.dashboard.nse_constituents.fetch_constituent_csv`)
  with a last-good disk-cache fallback
  (:mod:`brutex.dashboard.ntm_cache`); the Groww master itself carries NO index
  membership (Golden Rule #1);
* each constituent is matched to its Groww **NSE cash-equity** row by the COMMON
  vendor-neutral resolver (:func:`brutex.universe_constituents.resolve_constituents`
  — ISIN-primary, ticker / ``trading_symbol`` fallback, O(1) per lookup);
* the **non-F&O** matches are admitted as REFERENCE_ONLY (tagged on the
  ``reference_only`` column); a matched constituent that is ALSO F&O-eligible
  stays ``reference_only=FALSE`` so it keeps its real option chain
  (DEC-OPTIONS-PULL-GUARD-001).

The same ``reference_only`` column also tags every **spot-only index** — an ``IDX``
with no listed options of its own that is not a brute-force index (NIFTY IT / 100 /
TOTAL MARKET, INDIA VIX) — so the ``/universe`` dashboard does NOT badge it
option-pullable (DEC-NON-OPTION-INDEX-REFTAG-001). The options-bearing indices
(NIFTY / BANKNIFTY / FINNIFTY …) keep their chain; the brute-force trio (NIFTY /
BANKNIFTY / SENSEX) is always tradable, never tagged reference_only.

HARD SCOPE GUARD (CLAUDE.md §3): NTM constituents are lake / REFERENCE_ONLY only
— NEVER in the 52-condition vocabulary or the brute-force engine, which stays
exactly NIFTY / BANKNIFTY / SENSEX (locked by
DEC-ENGINE-ISOLATION-INVARIANT-001). On a network miss with no cache the load
degrades HONESTLY to the F&O-eligible base set with a ``reason`` note (never a
500, never invented constituents — Golden Rule #1).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import structlog

from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get
from brutex.adapters.groww.instruments import InstrumentIndex, index_symbol_of
from brutex.dashboard.nse_constituents import fetch_constituent_csv, parse_constituents
from brutex.dashboard.ntm_cache import (
    default_cache_dir,
    load_cached_constituent_csv,
    save_constituent_csv,
)
from brutex.dashboard.universe_query import universe_summary
from brutex.models import Exchange, Instrument, InstrumentType
from brutex.universe import (
    OptionUnderlyingGap,
    UnderlyingEconomics,
    build_fno_eligible_underlyings,
    build_options_eligible_underlyings,
    build_underlying_economics,
    diagnose_unmatched_option_underlyings,
    is_reference_only,
    is_spot_only_index,
    is_universe_underlying,
)
from brutex.universe_constituents import resolve_constituents

_LOG = structlog.get_logger(__name__)

#: The index NAME whose constituent CSV is fetched for NTM enrichment — the key
#: :func:`~brutex.dashboard.nse_constituents.constituent_url` maps to the
#: operator-verified niftyindices URL (DEC-NIFTY-TOTAL-MARKET-URL-001). Every
#: spelling alias ("NIFTY TOTAL MKT", "NIFTYTOTALMARKET", …) resolves to the same
#: CSV, so the canonical full name is sufficient here.
_NTM_INDEX_NAME = "NIFTY TOTAL MARKET"

#: Max gaps inlined in the ``universe.load.fno_index_unmatched`` structlog line
#: (DEC-FNO-INDEX-MATCH-DIAG-001). The full count is always logged via ``count``;
#: only the inlined ``gaps`` list is capped so a pathological master can never
#: bloat one log line. 50 is far above the handful of real F&O indices.
_FNO_INDEX_GAP_LOG_CAP = 50

#: Serialises concurrent keyless loads (DEC-UNDERLYING-ECONOMICS-001). Two
#: overlapping ``load-instruments`` POSTs each do a full-snapshot upsert which
#: wipes the whole snapshot_date before re-inserting; interleaving a wipe
#: between another call's wipe and insert left the lake with indices but 0
#: stocks ("25 indices · 0 stocks"). This module-level lock makes the
#: download+parse+upsert+read-back atomic w.r.t. other loads in the same
#: process, so a wipe can never interleave another load's insert. The
#: client-side in-flight guard (universe.html) prevents the races at the
#: source; this lock is the server-side belt-and-braces.
_LOAD_LOCK: threading.Lock = threading.Lock()


def _resolve_constituent_csv(
    opener: Callable[[str], str] | None,
    *,
    cache_dir: Path,
) -> tuple[str, bool]:
    """Live NTM constituent fetch with a last-good disk-cache fallback.

    DEC-NTM-CONSTITUENT-CACHE-001. Returns ``(csv_text, from_cache)``:

    * a successful NON-EMPTY live fetch → ``(live, False)`` and REFRESHES the cache
      (so the next outage has a fresh fallback);
    * a live failure (``niftyindices`` timeout / bot-block) OR an empty live result
      → fall back to the cache → ``(cached, True)`` when a cache exists, else
      ``("", False)`` (no live source AND no cache).

    The live ``OSError`` is swallowed ONLY when a cache can cover it; with NO cache
    the exception PROPAGATES so the caller's single fail-soft handler notes why
    (keeping one tested except site instead of a provably-dead nested one).
    """
    try:
        live = fetch_constituent_csv(_NTM_INDEX_NAME, opener=opener) or ""
    except OSError:
        cached = load_cached_constituent_csv(cache_dir=cache_dir)
        if cached:
            return cached, True
        raise  # no cache to cover it → let the caller's fail-soft handler note why
    if live:
        save_constituent_csv(live, cache_dir=cache_dir)
        return live, False
    cached = load_cached_constituent_csv(cache_dir=cache_dir)
    return (cached, True) if cached else ("", False)


def _ntm_admission(
    index: InstrumentIndex,
    *,
    fno_eligible: frozenset[tuple[Exchange, str]],
    constituent_opener: Callable[[str], str] | None,
    cache_dir: Path,
) -> tuple[frozenset[int], dict[str, Any]]:
    """Resolve NIFTY-Total-Market constituents → REFERENCE_ONLY ``SECURITY_ID``s.

    Mirrors the retired DEC-NIFTY-TOTAL-MARKET-LOAD-001 semantics against the Groww
    :class:`InstrumentIndex` (DEC-NTM-GROWW-REPOINT-001): fetch NTM's constituent
    CSV (best-effort + last-good cache), match each member to its Groww NSE
    cash-equity row by the COMMON resolver, and return the NON-F&O matches as the
    set to ADMIT + TAG ``reference_only=TRUE``.

    Returns ``(reference_security_ids, note)``. ``note`` always carries
    ``"admitted"``; on a skip it adds a ``"reason"``; on success it adds the
    coverage counts (``constituents`` / ``matched`` / ``reference`` / ``unmatched``
    / ``from_cache``) for the operator's at-a-glance audit.

    **Fail-soft (Golden Rule #1 + #10):** any miss (no equities to match,
    niftyindices unreachable with no cache, an empty CSV) returns an EMPTY set + an
    honest ``reason`` — a network hiccup NEVER breaks the keyless core load, and we
    NEVER invent constituents. ``require_full_coverage`` is implicitly ``False``
    here: a constituent the Groww master does not list is reported in ``unmatched``,
    not admitted.
    """
    note: dict[str, Any] = {"admitted": False}

    # An NTM constituent can only ever be an NSE cash equity, so with none in the
    # master there is nothing to match — skip the niftyindices fetch entirely.
    nse_equities = [
        inst
        for inst in index.universe
        if inst.instrument == InstrumentType.EQ and inst.exch_id == Exchange.NSE
    ]
    if not nse_equities:
        note["reason"] = "no NSE cash equities in the master to match NTM constituents against"
        return frozenset(), note

    # F&O-eligible NSE-equity SECURITY_IDs stay reference_only=FALSE even when they
    # are ALSO NTM constituents — they keep their real F&O option chain
    # (DEC-OPTIONS-PULL-GUARD-001). Only the NON-F&O matches become reference rows.
    fno_security_ids = frozenset(
        inst.security_id
        for inst in nse_equities
        if (inst.exch_id, inst.symbol_name) in fno_eligible
    )

    # Best-effort live NTM constituent CSV with a last-good disk-cache fallback
    # (niftyindices.com bot-blocks / times out plain HTTP clients — a live failure
    # would otherwise crater the universe from ~750 to the F&O-eligible base set).
    try:
        csv_text, from_cache = _resolve_constituent_csv(constituent_opener, cache_dir=cache_dir)
    except OSError as exc:
        note["reason"] = (
            "NIFTY Total Market constituents need niftyindices.com, currently "
            f"unreachable ({exc}) with no last-good cache — the universe falls back "
            "to the F&O-eligible base set. One-time fix: download "
            "ind_niftytotalmarket_list.csv in your browser and drop it at "
            "~/.brutex/cache/ntm_constituents.csv."
        )
        return frozenset(), note
    if not csv_text:
        note["reason"] = (
            "no NIFTY Total Market constituent CSV available (live source returned "
            "nothing and no last-good cache) — the universe falls back to the "
            "F&O-eligible base set"
        )
        return frozenset(), note

    # Match each constituent → its Groww NSE cash-equity SECURITY_ID (ISIN-primary,
    # ticker/trading_symbol fallback) via the COMMON vendor-neutral resolver, then
    # keep ONLY the non-F&O matches as the new REFERENCE_ONLY admissions.
    report = resolve_constituents(parse_constituents(csv_text), index.universe)
    reference_security_ids = frozenset(report.security_ids) - fno_security_ids

    note.update(
        admitted=bool(reference_security_ids),
        constituents=report.total,
        matched=len(report.matched),
        reference=len(reference_security_ids),
        unmatched=len(report.unmatched),
        from_cache=from_cache,
    )
    if from_cache:
        note["constituent_note"] = (
            "live NTM constituent source unavailable — used last-good cache "
            "(~/.brutex/cache/ntm_constituents.csv)"
        )
    return reference_security_ids, note


def _groww_symbol_index_bridge(
    index: InstrumentIndex,
    options_eligible: frozenset[tuple[Exchange, str]],
) -> tuple[frozenset[tuple[Exchange, str]], frozenset[tuple[Exchange, str]]]:
    """Bridge each spot ``IDX`` row to its options via its ``groww_symbol``.

    DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001. The OPT-vs-REF badge keys on an EXACT
    ``(exchange, symbol_name)`` match against *options_eligible* (the option
    legs' ``underlying_symbol`` set — DEC-NON-OPTION-INDEX-REFTAG-001). That
    silently drops a genuine F&O index to REFERENCE_ONLY whenever its ``IDX``
    row carries a VERBOSE ``trading_symbol`` (``symbol_name``, e.g.
    ``"NIFTY MID SELECT"`` / ``"NIFTY NEXT 50"``) that differs from the
    compact F&O symbol the vendor stamps on its option legs
    (``"MIDCPNIFTY"`` / ``"NIFTYNXT50"``) — the live ``make run`` proved both as
    ``count=2`` gaps with ``nearest_index=None`` (normalization cannot bridge
    ``NXT`` ↔ ``NEXT`` or ``MIDCP`` ↔ ``MIDCAP SELECT``).

    The vendor-CANONICAL bridge is the ``IDX`` row's own ``groww_symbol`` key:
    its SYMBOL component (:func:`~brutex.adapters.groww.instruments.index_symbol_of`,
    ``"NSE-MIDCPNIFTY"`` → ``"MIDCPNIFTY"``) IS the vendor's F&O symbol — the
    SAME string the option legs name as ``underlying_symbol`` (the brute-force
    universe proves this convention: the ``NSE-NIFTY`` spot row ↔ option
    ``underlying_symbol`` ``"NIFTY"``). This reads the vendor's OWN identity, so
    it is NOT a hardcoded per-index alias (Golden Rule #1).

    Returns a ``(options_eligible_bridged, index_alias_symbols)`` pair:

    * ``options_eligible_bridged`` — *options_eligible* UNION the
      ``(exchange, symbol_name)`` of every ``IDX`` whose ``groww_symbol`` F&O
      symbol IS options-eligible, so the unchanged
      :func:`~brutex.universe.is_spot_only_index` symbol_name check now matches
      such an index → it keeps its OPT badge (``reference_only=FALSE``).
    * ``index_alias_symbols`` — ``(exchange, groww_symbol_F&O_symbol)`` for
      EVERY ``IDX`` row, fed to
      :func:`~brutex.universe.diagnose_unmatched_option_underlyings` so it stops
      reporting an underlying the bridge already resolved (keeps the diagnostic
      honest — no permanent false ``count`` after the fix).

    Parameters
    ----------
    index
        The freshly parsed Groww :class:`InstrumentIndex` (its
        ``by_groww_symbol`` keys carry the vendor ``"EXCH-SYMBOL"`` strings,
        only available here before the lean ingest discards the legs).
    options_eligible
        The options-bearing ``(exchange, underlying_symbol)`` set from
        :func:`~brutex.universe.build_options_eligible_underlyings`.

    Returns
    -------
    tuple[frozenset[tuple[Exchange, str]], frozenset[tuple[Exchange, str]]]
        ``(options_eligible_bridged, index_alias_symbols)``.
    """
    index_alias_symbols: set[tuple[Exchange, str]] = set()
    bridged: set[tuple[Exchange, str]] = set()
    for groww_symbol, inst in index.by_groww_symbol.items():
        if inst.instrument is not InstrumentType.IDX:
            continue
        f_and_o_symbol = index_symbol_of(groww_symbol)
        if not f_and_o_symbol:  # a groww_symbol with no "-" → nothing to bridge.
            continue
        alias = (inst.exch_id, f_and_o_symbol)
        index_alias_symbols.add(alias)
        if alias in options_eligible:
            bridged.add((inst.exch_id, inst.symbol_name))
    return frozenset(options_eligible | bridged), frozenset(index_alias_symbols)


def _reference_only_of(
    reference_security_ids: frozenset[int],
    options_eligible: frozenset[tuple[Exchange, str]],
) -> Callable[[Instrument], bool]:
    """Build the ``reference_only`` upsert predicate.

    Returns a callable that is ``True`` for any of the three REFERENCE_ONLY arms —
    the single, unified encoding the lean upsert tags onto the ``reference_only``
    column:

    1. **India VIX** — the index-level :func:`brutex.universe.is_reference_only`
       flag (DEC-NIFTY-TOTAL-MARKET-REFTAG-001). Kept first so replacing the
       store's default predicate can never drop the VIX tag.
    2. **An admitted NTM constituent** — ``security_id`` in
       *reference_security_ids* (DEC-NTM-GROWW-REPOINT-001).
    3. **A spot-only index** — an ``IDX`` with no listed options that is not a
       brute-force index (:func:`brutex.universe.is_spot_only_index` against
       *options_eligible*), i.e. a sectoral / broad-market index like NIFTY IT /
       100 / TOTAL MARKET (DEC-NON-OPTION-INDEX-REFTAG-001). This closes the bug
       where every non-F&O index wrongly showed "OPT" + survived the dashboard's
       "F&O only" filter, while the options-bearing indices (NIFTY / BANKNIFTY /
       FINNIFTY / MIDCPNIFTY / NIFTYNXT50 …) keep their pullable chain.

    Parameters
    ----------
    reference_security_ids
        ``SECURITY_ID``s of the admitted non-F&O NTM constituents.
    options_eligible
        The options-bearing ``(exchange, underlying_symbol)`` set — the
        groww-symbol-BRIDGED variant from :func:`_groww_symbol_index_bridge`
        (DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001), i.e.
        :func:`brutex.universe.build_options_eligible_underlyings` (harvested at
        ingest before the legs are discarded) UNION the ``symbol_name`` of every
        ``IDX`` whose ``groww_symbol`` F&O symbol is options-eligible, so an
        index with a verbose ``trading_symbol`` (MIDCPNIFTY / NIFTYNXT50) still
        matches the unchanged ``is_spot_only_index`` symbol_name check.
    """

    def _is_reference_only(instr: Instrument) -> bool:
        """True for India VIX, an admitted NTM constituent, or a spot-only index."""
        return (
            is_reference_only(instr)
            or instr.security_id in reference_security_ids
            or is_spot_only_index(instr, options_eligible)
        )

    return _is_reference_only


def _log_fno_index_gaps(gaps: list[OptionUnderlyingGap]) -> None:
    """Emit ONE ``universe.load.fno_index_unmatched`` line when *gaps* is non-empty.

    DEC-FNO-INDEX-MATCH-DIAG-001 — the observe-only sink for
    :func:`brutex.universe.diagnose_unmatched_option_underlyings`. When at
    least one NSE option underlying looks like an index but matches no IDX
    spot row, this logs the full ``count`` plus an inlined ``gaps`` list
    (capped at :data:`_FNO_INDEX_GAP_LOG_CAP` so one master can never bloat a
    log line) of ``{underlying, nearest_index, exchange}`` dicts. It changes
    NO classification — a non-``None`` ``nearest_index`` flags a pure
    case/space mismatch, ``None`` a genuine abbreviation mismatch — so the
    operator can report the real strings for a SEPARATE data-driven fix.
    Nothing is logged when *gaps* is empty (the healthy steady state).

    Parameters
    ----------
    gaps
        The deterministic gap list from
        :func:`brutex.universe.diagnose_unmatched_option_underlyings`.
    """
    if not gaps:
        return
    _LOG.info(
        "universe.load.fno_index_unmatched",
        count=len(gaps),
        gaps=[
            {
                "underlying": gap.underlying_symbol,
                "nearest_index": gap.nearest_index_symbol_name,
                "exchange": gap.exchange.value,
            }
            for gap in gaps[:_FNO_INDEX_GAP_LOG_CAP]
        ],
    )


def _summarise_stored(store: Any) -> dict[str, int]:
    """Read the stored universe underlyings back and summarise the index/stock split.

    Lean-store-correct (DEC-INSTRUMENT-UNIVERSE-FAST-001): the store holds ONLY the
    universe underlyings (no option/future legs), so summarise from the IDX + EQ
    candidates plus the SQL-derived eligible set — the COMMON ``universe_summary``
    classifier. A REFERENCE_ONLY constituent counts as a "stock" here (every
    persisted NSE EQ is eligible-by-storage), so the operator sees the full
    ~750-name universe.
    """
    eligible = store.fno_eligible_underlyings()
    candidates = store.list_index_equity_instruments()
    return universe_summary(candidates, fno_eligible=eligible)


def _fold_leg_economics(
    instruments: list[Instrument],
    economics: dict[tuple[Exchange, str], UnderlyingEconomics],
) -> list[Instrument]:
    """Stamp each underlying with its TRADABLE F&O lot + freeze from its legs.

    DEC-GROWW-LEG-ECONOMICS-001 — the Groww twin of the retired-vendor
    ``DEC-UNDERLYING-LEG-ECONOMICS-001`` fold. In the Groww master an IDX / EQ
    row carries only its CASH economics (placeholder ``lot_size = 1`` for an
    equity, ``0`` for an index); the real *tradable* F&O lot + freeze live on the
    derivative legs. *economics* maps each ``(exchange, underlying_symbol)`` to
    ``(lot_size, tick_size, freeze_quantity)`` harvested once from those legs by
    :func:`brutex.universe.build_underlying_economics`. For every underlying with
    a leg carrying a real lot (``> 0``) we SUPERSEDE its lot + freeze with the
    leg's, but KEEP the row's own ``tick_size`` (a spot tick differs from its
    future's). A non-F&O underlying (no legs — a reference-only NTM constituent
    or a non-F&O index) is returned unchanged. A leg whose own freeze is blank
    leaves the underlying row's freeze intact. The leg's ``lot_size`` is always
    ``> 0`` (``Instrument.__post_init__`` enforces it for every FUT / CE / PE), so
    a folded equity can never violate the ``lot_size > 0`` invariant.

    Without this fold every F&O equity keeps the placeholder ``lot_size = 1``,
    which leaves
    :meth:`~brutex.data.instrument_store.InstrumentStore.has_placeholder_stock_economics`
    permanently ``True`` and makes the dashboard auto-heal
    (:func:`brutex.dashboard.auto_pull.should_reload_instrument_master`) re-load
    the master on every startup — a silent reload loop this fold closes.
    """
    folded: list[Instrument] = []
    for inst in instruments:
        leg = economics.get((inst.exch_id, inst.symbol_name))
        if leg is None:
            folded.append(inst)  # non-F&O underlying (no legs) → keep own economics
            continue
        # Supersede lot + freeze (keep the row's own spot tick). A leg without its
        # own freeze leaves the underlying row's freeze untouched.
        lot_size, freeze_qty = leg[0], leg[2]
        new_freeze = freeze_qty if freeze_qty is not None else inst.sm_freeze_qty
        folded.append(replace(inst, lot_size=lot_size, sm_freeze_qty=new_freeze))
    return folded


def _lean_universe(
    index: InstrumentIndex,
    *,
    fno_eligible: frozenset[tuple[Exchange, str]],
    reference_security_ids: frozenset[int],
) -> tuple[list[Instrument], int]:
    """Select the LEAN universe to persist + count the dropped derivative legs.

    The Groww :class:`InstrumentIndex` carries every in-scope row INCLUDING the
    FUT / CE / PE legs. The lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001)
    persists ONLY the universe underlyings — every admitted IDX, every
    F&O-eligible EQ, and every admitted REFERENCE_ONLY NTM constituent (an EQ
    whose ``security_id`` is in *reference_security_ids*) — and discards the legs
    (they are parsed only to discover which equities are F&O-eligible). Returns
    ``(lean_rows, legs_dropped)`` where ``legs_dropped`` is the count of in-scope
    rows NOT persisted (the legs + any non-admitted non-F&O equity), so the caller
    can report the accounting. The admission rule is delegated to the COMMON
    :func:`brutex.universe.is_universe_underlying` (CLAUDE.md §11 COMMON).
    """
    lean = [
        inst
        for inst in index.universe
        if is_universe_underlying(inst, fno_eligible, reference_security_ids)
    ]
    legs_dropped = len(index.universe) - len(lean)
    return lean, legs_dropped


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    constituent_opener: Callable[[str], str] | None = None,
    constituent_cache_dir: Path | None = None,
) -> dict[str, Any]:
    """Keylessly download + parse the Groww instrument master into *store*.

    Downloads the public Groww instrument-master CSV (NO Groww token), parses it
    into the O(1) :class:`~brutex.adapters.groww.instruments.InstrumentIndex`,
    resolves the NIFTY-Total-Market constituents to REFERENCE_ONLY admissions
    (best-effort — DEC-NTM-GROWW-REPOINT-001), stamps each underlying with its
    tradable F&O lot + freeze harvested from its derivative legs
    (DEC-GROWW-LEG-ECONOMICS-001), full-snapshot upserts the LEAN universe
    (IDX + F&O-eligible EQ + the matched REFERENCE_ONLY constituents) into *store*,
    prunes to the newest snapshot, then reads the snapshot back and returns
    headline counts for the ``/universe`` page.

    Parameters
    ----------
    store
        A :class:`~brutex.data.instrument_store.InstrumentStore` (or a
        duck-typed sink exposing ``upsert_many`` + ``prune_to_latest_snapshot``
        + ``fno_eligible_underlyings`` + ``list_index_equity_instruments``).
    http_get
        Injected ``url -> CSV text`` for offline tests. When ``None`` (the
        production path), the real SSRF-guarded, keyless public-CSV fetch
        (:func:`~brutex.adapters.groww.instrument_loader.urllib_http_get`,
        restricted to the Groww asset host) is used.
    run_id_factory
        Optional ``() -> run_id`` stamped on the upsert for audit lineage.
    constituent_opener
        Injected ``url -> CSV text`` for the NTM constituent fetch (offline
        tests). When ``None`` (production) the real best-effort niftyindices
        opener is used. The fetch runs only when the master carries NSE
        equities to match against, so an index-only / empty master never touches
        the network here.
    constituent_cache_dir
        Directory for the last-good NTM constituent CSV cache
        (DEC-NTM-CONSTITUENT-CACHE-001). ``None`` (production) uses
        :func:`~brutex.dashboard.ntm_cache.default_cache_dir` (``~/.brutex/cache/``).
        A successful live fetch refreshes the cache; a failed/empty fetch falls back
        to it, so a transient niftyindices timeout no longer craters the universe to
        the base set. The operator can also seed the fallback by dropping the
        browser-downloaded ``ind_niftytotalmarket_list.csv`` at
        ``<cache_dir>/ntm_constituents.csv``. Tests inject a tmp dir for isolation.

    Returns
    -------
    dict[str, Any]
        ``{"parsed", "persisted", "legs_dropped", "filtered", "quarantined",
        "index_count", "stock_count", "total", "ntm"}`` — the load accounting
        plus the index/stock split from :func:`universe_summary` over the stored
        universe underlyings. ``filtered`` is always ``0`` (the Groww parser
        skips out-of-scope MCX / COMMODITY rows BEFORE they reach the index, so
        ``parsed`` already counts only in-scope rows) and ``quarantined`` is
        always ``0`` (the Groww parser **fails closed** on a malformed in-scope
        row — Golden Rule #1 — rather than quarantining). ``ntm`` is the
        NIFTY-Total-Market admission note (``{"admitted": bool, ...}`` —
        DEC-NTM-GROWW-REPOINT-001).

    Raises
    ------
    brutex.models.InstrumentFormatError
        On CSV header schema drift (the Groww 21-column header is enforced) or
        any malformed in-scope row — the store keeps its last good snapshot.
    """
    fetch = http_get
    if fetch is None:
        fetch = urllib_http_get  # pragma: no cover — SSRF-guarded network path
    run_id = (run_id_factory or (lambda: "instruments-load"))()

    # Serialise the whole refresh + read-back under the module lock so two
    # concurrent loads cannot interleave a snapshot wipe with another's insert
    # (DEC-UNDERLYING-ECONOMICS-001 — the "25 indices · 0 stocks" corruption).
    with _LOAD_LOCK:
        # Fetch + parse the master ONCE (the Groww loader SSRF-checks the URL and
        # builds the O(1) in-memory index). The header is enforced inside the
        # parser, which fails loud on schema drift (Golden Rule #1).
        master_text = fetch(load_index_url())
        csv_sha256 = hashlib.sha256(master_text.encode("utf-8")).hexdigest()
        index = load_index(lambda _url: master_text)
        parsed = len(index.universe)
        fno_eligible = build_fno_eligible_underlyings(index.universe)
        # The options-bearing underlyings (CE/PE legs) — harvested NOW, while the
        # full master still carries the legs (the lean ingest discards them). Used
        # to tag spot-only indices reference_only so the dashboard never badges a
        # non-option index "OPT" (DEC-NON-OPTION-INDEX-REFTAG-001).
        options_eligible = build_options_eligible_underlyings(index.universe)
        # Bridge each IDX spot row to its options via its groww_symbol F&O
        # symbol (DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001) — so an index whose
        # verbose trading_symbol differs from the option legs' underlying_symbol
        # (MIDCPNIFTY / NIFTYNXT50) keeps its OPT badge instead of dropping to
        # REF. The bridge reads the vendor's OWN groww_symbol identity, never a
        # hardcoded alias (Golden Rule #1).
        options_eligible_bridged, index_alias_symbols = _groww_symbol_index_bridge(
            index, options_eligible
        )

        # OBSERVE-ONLY diagnostic (DEC-FNO-INDEX-MATCH-DIAG-001): surface any
        # NSE option underlying that LOOKS like an index but matches no IDX
        # spot row even AFTER the groww_symbol bridge — a residual gap the
        # bridge could not resolve (logged with the real strings, changes no
        # classification). Indices the bridge fixed are passed as
        # index_alias_symbols so they are no longer reported (honest count).
        _log_fno_index_gaps(
            diagnose_unmatched_option_underlyings(index.universe, index_alias_symbols)
        )

        # NTM enrichment is fail-soft — it never blocks the keyless core load
        # (DEC-NTM-GROWW-REPOINT-001). The returned ids ADMIT the non-F&O
        # constituents into the lean set AND tag them reference_only at upsert.
        reference_security_ids, ntm_note = _ntm_admission(
            index,
            fno_eligible=fno_eligible,
            constituent_opener=constituent_opener,
            cache_dir=(
                constituent_cache_dir if constituent_cache_dir is not None else default_cache_dir()
            ),
        )
        if ntm_note.get("admitted"):
            _LOG.info("universe.load.ntm_admitted", **ntm_note)
        else:
            _LOG.info("universe.load.ntm_skipped", **ntm_note)

        # Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001): persist ONLY the
        # universe underlyings (IDX + F&O-eligible EQ + REFERENCE_ONLY NTM
        # constituents); the FUT / CE / PE legs are parsed for eligibility then
        # dropped. Full-snapshot replace so the day's rows are EXACTLY the lean
        # set (idempotent on re-pull).
        lean, legs_dropped = _lean_universe(
            index, fno_eligible=fno_eligible, reference_security_ids=reference_security_ids
        )
        # Stamp each underlying with its TRADABLE F&O lot + freeze from its legs
        # (DEC-GROWW-LEG-ECONOMICS-001) — the legs carry the real economics; the
        # IDX / EQ row carries only its cash placeholder. This also keeps
        # ``has_placeholder_stock_economics`` False so the auto-heal does not loop.
        lean = _fold_leg_economics(lean, build_underlying_economics(index.universe))
        store.upsert_many(
            lean,
            snapshot_date=date.today(),
            run_id=run_id,
            csv_source_sha256=csv_sha256,
            replace_snapshot=True,
            reference_only_of=_reference_only_of(reference_security_ids, options_eligible_bridged),
        )
        pruned = store.prune_to_latest_snapshot()
        summary = _summarise_stored(store)

    _LOG.info(
        "universe.load.groww",
        run_id=run_id,
        parsed=parsed,
        persisted=len(lean),
        legs_dropped=legs_dropped,
        pruned=pruned,
        index_count=summary["index_count"],
        stock_count=summary["stock_count"],
        ntm_reference=ntm_note.get("reference", 0),
    )
    return {
        "parsed": parsed,
        "persisted": len(lean),
        "legs_dropped": legs_dropped,
        # The Groww parser skips out-of-scope rows BEFORE the index, so they
        # never reach this layer — `parsed` already counts only in-scope rows.
        "filtered": 0,
        # Groww fails closed on a malformed in-scope row — no quarantine bucket.
        "quarantined": 0,
        "index_count": summary["index_count"],
        "stock_count": summary["stock_count"],
        "total": summary["total"],
        # NTM admission status (DEC-NTM-GROWW-REPOINT-001) — surfaced so the
        # operator SEES whether NIFTY Total Market's constituents were admitted
        # this load (and, if skipped, the honest reason).
        "ntm": ntm_note,
    }


def load_index_url() -> str:
    """Return the Groww instrument-master CSV URL (the keyless public asset).

    A thin indirection over
    :data:`~brutex.adapters.groww.constants.GROWW_INSTRUMENT_CSV_URL` so the load
    site reads naturally and the constant has exactly one import site here.
    """
    from brutex.adapters.groww.constants import GROWW_INSTRUMENT_CSV_URL

    return GROWW_INSTRUMENT_CSV_URL


__all__ = ["load_instrument_master"]
