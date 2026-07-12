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
(:func:`~brutex.adapters.groww.instrument_loader.load_index_with_report` →
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
import json
import os
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.adapters.groww.instrument_loader import load_index_with_report, urllib_http_get
from brutex.adapters.groww.instruments import (
    PARSE_SKIPS_META_KEY as _PARSE_SKIPS_META_KEY,
)
from brutex.adapters.groww.instruments import (
    InstrumentIndex,
    ParseSkipReport,
    index_symbol_of,
    parse_skip_threshold,
    parse_skip_verdict,
)
from brutex.dashboard.nse_constituents import fetch_constituent_csv, parse_constituents
from brutex.dashboard.ntm_cache import (
    CachedConstituents,
    default_cache_dir,
    load_cached_constituent_with_age,
    revalidate_lock,
    save_constituent_csv,
)
from brutex.dashboard.universe_query import universe_summary
from brutex.data.economics_integrity import (
    META_KEY as _ECONOMICS_INTEGRITY_META_KEY,
)
from brutex.data.economics_integrity import (
    audit_economics_integrity,
)
from brutex.data.economics_integrity import (
    report_to_dict as _economics_report_to_dict,
)
from brutex.data.master_integrity import (
    META_KEY as _MASTER_INTEGRITY_META_KEY,
)
from brutex.data.master_integrity import (
    audit_master_integrity,
    report_to_dict,
)
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

#: Sourced spot-``trading_symbol`` → NSE F&O ``underlying_symbol`` equivalence for
#: the two genuine NSE index derivatives whose Groww spot ``trading_symbol`` differs
#: from the symbol the vendor stamps on their option legs — AND whose own
#: ``groww_symbol`` ALSO carries the spot spelling, so the groww_symbol bridge
#: (:func:`_groww_symbol_index_bridge`) cannot resolve them either
#: (DEC-FNO-INDEX-ALIAS-001).
#:
#: Source — NSE's OWN index-derivatives product naming (Golden Rule #1, a SOURCED
#: vendor equivalence, NOT invented membership):
#:
#: * "Nifty Next 50" — NSE index F&O symbol ``NIFTYNXT50``; the Groww spot
#:   ``trading_symbol`` observed in the operator's live ``make run`` is ``NIFTYJR``.
#: * "Nifty Midcap Select" — NSE index F&O symbol ``MIDCPNIFTY``; the Groww spot
#:   ``trading_symbol`` observed in the operator's live ``make run`` is
#:   ``NIFTYMIDSELECT``.
#:
#: Keyed by spot ``trading_symbol`` (the ``IDX`` row's ``symbol_name``); the value
#: is the F&O ``underlying_symbol`` to test against the options-eligible set. The
#: live ``universe.load.fno_index_unmatched count=2 gaps=[MIDCPNIFTY, NIFTYNXT50]``
#: log is exactly these two — after this fix the gap count drops to 0.
_SOURCED_FNO_INDEX_ALIASES: Final[dict[str, str]] = {
    "NIFTYJR": "NIFTYNXT50",
    "NIFTYMIDSELECT": "MIDCPNIFTY",
}

#: Operator JSON override env for :data:`_SOURCED_FNO_INDEX_ALIASES`
#: (``{spot_trading_symbol: fno_underlying_symbol}``) — MERGED on top of the sourced
#: defaults (operator entries win). Mirrors ``BRUTEX_INDEX_CSV_FILENAMES``
#: (DEC-CONSTITUENTS-OVERRIDE-001): a future such index needs no code change
#: (CLAUDE.md §11 DYNAMIC). Parsed defensively — absent / malformed → ignored.
FNO_INDEX_ALIASES_OVERRIDE_ENV: Final[str] = "BRUTEX_FNO_INDEX_ALIASES"

#: NTM constituent-cache freshness TTL (seconds) for the CACHE-FIRST load
#: (DEC-NTM-CACHE-FIRST-001). A cache YOUNGER than this is served INSTANTLY with NO
#: network call — the ultra-fast O(1) hot path that keeps every ``/universe`` load
#: after the first instant (the operator's "why is this slow EVERY time" was the old
#: LIVE-FIRST path re-hitting the slow niftyindices.com host on every load even with
#: a good cache on disk). An older/absent cache triggers ONE bounded fail-fast live
#: refresh. NTM constituents rebalance only semi-annually (CLAUDE.md §7), so a 7-day
#: default is amply fresh while making the slow path rare. Env-tunable
#: (``BRUTEX_NTM_CACHE_TTL_SECONDS``); a non-numeric value falls back to the default
#: (never crashes). A non-positive value disables the fast path (always refresh) yet
#: still serves a stale cache as a fallback — so worst case is "patient as before",
#: never worse.
_NTM_CACHE_TTL_ENV: Final[str] = "BRUTEX_NTM_CACHE_TTL_SECONDS"
_NTM_CACHE_TTL_DEFAULT_SECONDS: Final[float] = 7 * 24 * 60 * 60  # 7 days


def _ntm_cache_ttl_seconds() -> float:
    """Return the NTM cache freshness TTL (seconds) — env-tunable, malformed → default. PURE.

    Reads :data:`_NTM_CACHE_TTL_ENV`. Absent or non-numeric falls back to
    :data:`_NTM_CACHE_TTL_DEFAULT_SECONDS` (a garbled override costs only the tuning,
    never a crash — reliability). A non-positive value is returned as-is so the
    cache-first comparison (``age < ttl`` with a clamped non-negative age) treats every
    cache as stale → always refresh (the explicit "disable the fast path" lever).
    """
    raw = os.environ.get(_NTM_CACHE_TTL_ENV)
    if not raw:
        return _NTM_CACHE_TTL_DEFAULT_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _NTM_CACHE_TTL_DEFAULT_SECONDS


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


#: Single-flight guard for the BACKGROUND NTM cache revalidation
#: (DEC-NTM-STALE-WHILE-REVALIDATE-001). Module-level by the SAME sanctioned precedent
#: as :data:`_LOAD_LOCK` above (a process-wide coordination primitive, documented — not
#: hidden business state): while one background refresh is in flight, a concurrent
#: post-TTL load serves its stale cache WITHOUT spawning a second refresh, so the slow
#: niftyindices host is hit AT MOST once per revalidation cycle per process. The
#: in-flight flag lives in a one-key mutable holder (mutated in place under the lock)
#: rather than a rebindable module global — same semantics, no ``global`` statement.
#: This layer is deliberately PROCESS-local (cheap, spawn-free); the CROSS-process
#: arm is the worker-side non-blocking :func:`~brutex.dashboard.ntm_cache
#: .revalidate_lock` flock (DEC-NTM-REVALIDATE-CROSSPROC-001), which closes the
#: multi-uvicorn-worker double-refresh named follow-up of #1287.
_NTM_REVALIDATE_LOCK: threading.Lock = threading.Lock()
_NTM_REVALIDATE_STATE: Final[dict[str, bool]] = {"in_flight": False}


def _ntm_cache_is_fresh(cached: CachedConstituents) -> bool:
    """``True`` iff *cached* is younger than the NTM TTL — the ONE freshness predicate.

    COMMON (§11): shared by the cache-first serve decision
    (:func:`_resolve_constituent_csv` path 1) and the revalidation worker's
    under-the-lock re-check (:func:`_spawn_ntm_revalidation`), so the two can never
    drift. A non-positive TTL (the "disable the fast path" lever) makes every cache
    read stale — both consumers inherit that pinned semantic. O(1).
    """
    return cached.age_seconds < _ntm_cache_ttl_seconds()


def _default_revalidate_runner(job: Callable[[], None]) -> None:
    """Run *job* on a named daemon thread — the production revalidation spawner.

    Daemon so a hung niftyindices fetch can never block interpreter exit; tests
    inject a synchronous/capturing runner instead (never a real thread).
    """
    threading.Thread(target=job, name="brutex-ntm-revalidate", daemon=True).start()


def _refresh_constituent_cache(opener: Callable[[str], str] | None, *, cache_dir: Path) -> str:
    """Fetch the live NTM CSV and persist a NON-EMPTY result — the ONE refresh body.

    COMMON (§11): shared verbatim by the synchronous truly-no-cache boot path and the
    background stale-while-revalidate worker, so the two can never drift. Returns the
    live text (``""`` when the source returned nothing — the cache is then left
    untouched); a network ``OSError`` PROPAGATES to the caller (the sync path lets it
    reach the fail-soft handler; the background worker swallows + logs it).
    The save is atomic (DEC-ATOMIC-WRITE-FINISH-001), so a reader never sees a torn
    cache file.
    """
    live = fetch_constituent_csv(_NTM_INDEX_NAME, opener=opener) or ""
    if live:
        save_constituent_csv(live, cache_dir=cache_dir)
    return live


def _spawn_ntm_revalidation(
    opener: Callable[[str], str] | None,
    *,
    cache_dir: Path,
    runner: Callable[[Callable[[], None]], None] | None,
) -> bool:
    """Kick ONE single-flight background NTM cache refresh; report whether it spawned.

    DEC-NTM-STALE-WHILE-REVALIDATE-001. Best-effort by construction: the worker
    swallows + logs EVERY error (``ntm.revalidate_failed``) and only ever touches the
    on-disk cache (atomic save) — it NEVER takes :data:`_LOAD_LOCK` and NEVER touches
    the instrument store, so it cannot deadlock or interleave a concurrent load. The
    in-flight flag is cleared in the worker's ``finally`` (and on a failed SPAWN), so
    a later post-TTL load can always revalidate again. Returns ``False`` when a
    refresh is already in flight (single-flight — no second spawn).

    CROSS-PROCESS single-flight (DEC-NTM-REVALIDATE-CROSSPROC-001): the worker takes
    the NON-BLOCKING :func:`~brutex.dashboard.ntm_cache.revalidate_lock` flock before
    fetching — a peer process already refreshing means SKIP (its atomic save lands
    for everyone; ``ntm.revalidate_skipped_crossproc``), and a genuinely-acquired
    lock RE-CHECKS freshness (double-checked: the peer may have JUST finished —
    ``ntm.revalidate_skipped_fresh``) so the slow niftyindices host is hit at most
    once per revalidation cycle ACROSS uvicorn workers too. An UNAVAILABLE lock
    (read-only cache dir) fails OPEN to the pre-fix unguarded refresh
    (``ntm.revalidate_lock_unavailable`` — idempotent-harmless, never worse).
    """
    with _NTM_REVALIDATE_LOCK:
        if _NTM_REVALIDATE_STATE["in_flight"]:
            return False
        _NTM_REVALIDATE_STATE["in_flight"] = True

    def _job() -> None:
        try:
            with revalidate_lock(cache_dir=cache_dir) as lock_verdict:
                if lock_verdict == "held_elsewhere":
                    # A PEER process is refreshing right now — skip, never double
                    # the fetch; the peer's atomic save is picked up by the next
                    # load (cross-process single-flight).
                    _LOG.info("ntm.revalidate_skipped_crossproc", cache_dir=str(cache_dir))
                    return
                if lock_verdict == "unavailable":
                    # Fail OPEN: no cross-process guard, but the refresh itself
                    # (the actual purpose) still runs — pre-fix behaviour.
                    _LOG.warning("ntm.revalidate_lock_unavailable", cache_dir=str(cache_dir))
                else:
                    # Lock ACQUIRED — double-checked freshness: a peer may have
                    # refreshed between our spawn decision and this acquire.
                    cached = load_cached_constituent_with_age(cache_dir=cache_dir)
                    if cached is not None and _ntm_cache_is_fresh(cached):
                        _LOG.info("ntm.revalidate_skipped_fresh", cache_dir=str(cache_dir))
                        return
                refreshed = _refresh_constituent_cache(opener, cache_dir=cache_dir)
                _LOG.info(
                    "ntm.revalidate_done",
                    refreshed=bool(refreshed),
                    cache_dir=str(cache_dir),
                )
        except Exception:  # pylint: disable=broad-exception-caught — best-effort background refresh
            _LOG.warning("ntm.revalidate_failed", cache_dir=str(cache_dir), exc_info=True)
        finally:
            with _NTM_REVALIDATE_LOCK:
                _NTM_REVALIDATE_STATE["in_flight"] = False

    run = _default_revalidate_runner if runner is None else runner
    try:
        run(_job)
    except Exception:  # pylint: disable=broad-exception-caught — a failed spawn must never break the load
        with _NTM_REVALIDATE_LOCK:
            _NTM_REVALIDATE_STATE["in_flight"] = False
        _LOG.warning("ntm.revalidate_spawn_failed", cache_dir=str(cache_dir), exc_info=True)
    return True


def _resolve_constituent_csv(
    opener: Callable[[str], str] | None,
    *,
    cache_dir: Path,
    revalidate_runner: Callable[[Callable[[], None]], None] | None = None,
) -> tuple[str, str]:
    """Resolve the NTM constituent CSV — CACHE-FIRST + **stale-while-revalidate**.

    DEC-NTM-CACHE-FIRST-001 made the fresh-cache load instant;
    DEC-NTM-STALE-WHILE-REVALIDATE-001 extends the same courtesy to the POST-TTL
    load: a cache OLDER than the TTL is served IMMEDIATELY (zero request-thread
    network wait — the old path blocked the ``/universe`` load ~30–61 s on the slow
    niftyindices host, under ``_LOAD_LOCK``) while ONE single-flight BACKGROUND
    refresh (:func:`_spawn_ntm_revalidation`) re-fetches + atomically re-saves the
    cache, so the NEXT load is a ``cache_fresh`` hit on the refreshed data. Only a
    truly-no-cache boot still pays the synchronous bounded fetch (there is nothing
    to serve meanwhile).

    Returns ``(csv_text, source)`` where ``source`` is one of:

    * ``"cache_fresh"`` — a cache younger than the TTL (:func:`_ntm_cache_ttl_seconds`)
      was served WITHOUT any network call (the fast hot path: one small ``stat``+read,
      no network — constant w.r.t. the slow niftyindices host, so every load after the
      first is instant; NOT a literal O(1) over the CSV, which is still parsed);
    * ``"cache_stale_revalidating"`` — a POST-TTL cache was served IMMEDIATELY and a
      single-flight background refresh was kicked (or is already in flight — the
      process-local flag; the worker additionally holds the CROSS-process
      :func:`~brutex.dashboard.ntm_cache.revalidate_lock` flock, so multiple uvicorn
      workers cannot double the fetch either — DEC-NTM-REVALIDATE-CROSSPROC-001).
      The data is honestly one cycle stale (NTM rebalances semi-annually —
      CLAUDE.md §7);
    * ``"live"`` — NO cache at all, so the synchronous bounded live fetch ran and
      succeeded; the cache is seeded (the next load is a ``cache_fresh`` hit);
    * ``"none"`` — no cache AND the live fetch returned empty → ``("", "none")``.

    A non-positive TTL (the "disable the fast path" lever) now means
    revalidate-in-background on EVERY load that has a cache — the cache is still
    always served instantly, never a synchronous re-fetch (pinned; the worst case is
    one background refresh per load, request latency never worse). With NO cache the
    live ``OSError`` PROPAGATES so the caller's single fail-soft handler notes the
    honest "unreachable, no cache" reason.
    """
    cached = load_cached_constituent_with_age(cache_dir=cache_dir)
    if cached is not None:
        # 1. FRESH cache → serve instantly, NO network (the ultra-fast hot path).
        if _ntm_cache_is_fresh(cached):
            return cached.csv_text, "cache_fresh"
        # 2. POST-TTL cache → serve IMMEDIATELY + kick ONE background refresh
        #    (stale-while-revalidate; single-flight — a refresh already in flight is
        #    not doubled). The request thread NEVER waits on niftyindices here.
        _spawn_ntm_revalidation(opener, cache_dir=cache_dir, runner=revalidate_runner)
        return cached.csv_text, "cache_stale_revalidating"
    # 3. Truly-no-cache boot → the synchronous bounded fail-fast live fetch (nothing
    #    to serve meanwhile). OSError propagates to the caller's fail-soft handler.
    live = _refresh_constituent_cache(opener, cache_dir=cache_dir)
    if live:
        return live, "live"
    return "", "none"


def _ntm_admission(
    index: InstrumentIndex,
    *,
    fno_eligible: frozenset[tuple[Exchange, str]],
    fno_eligible_security_ids: frozenset[int],
    constituent_opener: Callable[[str], str] | None,
    cache_dir: Path,
    revalidate_runner: Callable[[Callable[[], None]], None] | None = None,
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
    / ``from_cache`` / ``source``) for the operator's at-a-glance audit. ``source`` ∈
    ``{"cache_fresh", "live", "cache_stale_revalidating"}`` records WHY the CSV was
    served (the cache-first fast path vs a cold-boot live fetch vs the served-stale +
    background-refresh path — DEC-NTM-CACHE-FIRST-001 /
    DEC-NTM-STALE-WHILE-REVALIDATE-001; *revalidate_runner* is the injectable
    background spawner seam, ``None`` = a daemon thread).

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
    # This ticker key misses a SYMBOL-MISMATCHED F&O stock (spot symbol ≠ leg
    # underlying_symbol); *fno_eligible_security_ids* (the id-keyed token-join set)
    # catches those so they too stay tradable (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001).
    fno_security_ids = frozenset(
        inst.security_id
        for inst in nse_equities
        if (inst.exch_id, inst.symbol_name) in fno_eligible
    )

    # Best-effort live NTM constituent CSV with a last-good disk-cache fallback
    # (niftyindices.com bot-blocks / times out plain HTTP clients — a live failure
    # would otherwise crater the universe from ~750 to the F&O-eligible base set).
    try:
        csv_text, source = _resolve_constituent_csv(
            constituent_opener, cache_dir=cache_dir, revalidate_runner=revalidate_runner
        )
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
    # F&O-tradable WINS over NTM-reference: subtract BOTH the ticker-keyed F&O set
    # AND the id-keyed token-join F&O set, so a symbol-mismatched F&O stock that is
    # ALSO an NTM constituent is never tagged reference_only (it keeps its options).
    reference_security_ids = (
        frozenset(report.security_ids) - fno_security_ids - fno_eligible_security_ids
    )

    note.update(
        admitted=bool(reference_security_ids),
        constituents=report.total,
        matched=len(report.matched),
        reference=len(reference_security_ids),
        unmatched=len(report.unmatched),
        # ``from_cache`` is True for BOTH cache arms (fresh hot path + stale fallback);
        # ``source`` carries the honest, specific origin for the operator's audit.
        from_cache=source != "live",
        source=source,
    )
    if source == "cache_fresh":
        note["constituent_note"] = (
            "served the fresh last-good NTM constituent cache "
            "(~/.brutex/cache/ntm_constituents.csv) — no network call (fast path)"
        )
    elif source == "cache_stale_revalidating":
        note["constituent_note"] = (
            "served the post-TTL last-good NTM constituent cache IMMEDIATELY and "
            "kicked one background refresh (stale-while-revalidate) — the next load "
            "picks up the refreshed cache (~/.brutex/cache/ntm_constituents.csv)"
        )
    return reference_security_ids, note


def resolve_fno_index_aliases() -> dict[str, str]:
    """Return the spot→F&O index alias map: sourced defaults MERGED with the env override.

    DEC-FNO-INDEX-ALIAS-001 — the :data:`_SOURCED_FNO_INDEX_ALIASES` defaults
    (NSE-sourced ``NIFTYJR``→``NIFTYNXT50`` / ``NIFTYMIDSELECT``→``MIDCPNIFTY``)
    with the operator's :data:`FNO_INDEX_ALIASES_OVERRIDE_ENV` JSON merged ON TOP
    (operator entries win, mirroring ``BRUTEX_INDEX_CSV_FILENAMES``). Keys are
    upper-cased + trimmed spot ``trading_symbol``s; values are trimmed F&O
    ``underlying_symbol``s. The env is parsed defensively — absent, empty, non-JSON,
    a non-object, or a non-``str→non-blank-str`` pair is ignored (a lost / malformed
    override costs only the extra indices, never a crash — reliability).
    """
    aliases = dict(_SOURCED_FNO_INDEX_ALIASES)
    raw = os.environ.get(FNO_INDEX_ALIASES_OVERRIDE_ENV)
    if not raw:
        return aliases
    try:
        parsed = json.loads(raw)
    except ValueError:
        return aliases
    if not isinstance(parsed, dict):
        return aliases
    for key, value in parsed.items():
        if isinstance(key, str) and isinstance(value, str) and key.strip() and value.strip():
            aliases[key.strip().upper()] = value.strip()
    return aliases


#: Backward-compatible private alias for :func:`resolve_fno_index_aliases`, kept so
#: existing ``from ...instrument_loader import _resolve_fno_index_aliases`` callers
#: (the F&O-index diagnostic tests) keep resolving after the public rename
#: (DEC-FNO-UNDERLYING-RESOLVE-001 promoted it to the public API so the COMMON
#: F&O-underlying resolver can read the SAME map — no second source of truth).
_resolve_fno_index_aliases = resolve_fno_index_aliases


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


def _option_underlying_security_ids(index: InstrumentIndex) -> frozenset[int]:
    """Return the ``underlying_security_id`` set named by every NSE option leg.

    DEC-FNO-INDEX-TOKEN-JOIN-001 — the vendor-CANONICAL evidence for the
    token-join (5a): every ``CE`` / ``PE`` leg carries the NUMERIC
    ``underlying_security_id`` of its spot row (e.g. a RELIANCE option carries
    RELIANCE's numeric SECURITY_ID). A spot row whose own GENUINE numeric vendor
    token equals one of these IS option-pullable, immune to any symbol-spelling
    drift. Mirrors :func:`~brutex.universe.build_options_eligible_underlyings`
    (NSE option legs only — BSE contributes only the SENSEX index, no NSE-set
    derivatives) so the two evidence sources stay in lockstep.
    """
    return frozenset(
        inst.underlying_security_id
        for inst in index.universe
        if inst.is_option
        and inst.exch_id == Exchange.NSE
        and inst.underlying_security_id is not None
    )


def _has_genuine_numeric_token(instrument: Instrument) -> bool:
    """True iff *instrument*'s ``security_id`` is a GENUINE numeric vendor token.

    DEC-FNO-INDEX-TOKEN-JOIN-001. The Groww parser
    (:func:`~brutex.adapters.groww.instruments._security_id`) uses the raw CSV
    ``exchange_token`` directly when it is numeric, but hashes a NON-numeric index
    token (e.g. ``"NIFTY"`` / ``"NIFTYMIDSELECT"``) into a blake2b SURROGATE — which
    can NEVER joinably equal an option leg's numeric ``underlying_security_id``.
    The raw token is preserved on ``feed_token``, so a numeric ``feed_token`` ⇒
    ``security_id`` IS the real vendor token (a token-join is valid); a non-numeric
    or absent ``feed_token`` ⇒ a surrogate, so the token-join MUST be skipped for
    that row (it could otherwise spuriously collide). This is why stocks + BSE
    indices (numeric tokens) token-join cleanly while NSE indices (string tokens)
    do not.
    """
    token = instrument.feed_token
    return token is not None and token.isdigit()


def _classify_option_pullable(
    index: InstrumentIndex,
    options_eligible: frozenset[tuple[Exchange, str]],
) -> tuple[
    frozenset[tuple[Exchange, str]],
    frozenset[tuple[Exchange, str]],
    dict[str, list[str]],
    frozenset[int],
]:
    """Fold ALL three OPT-vs-REF bridges into one comprehensive classification.

    DEC-FNO-INDEX-ALIAS-001 — the union of three evidence sources that each rescue
    a spot row REF→OPT (``reference_only=FALSE``) when the plain
    ``(exchange, symbol_name)``-in-*options_eligible* check would miss it. Every
    rescued row's ``(exchange, symbol_name)`` is added to the returned bridged set
    so the UNCHANGED :func:`~brutex.universe.is_spot_only_index` symbol_name check
    matches it (the single, shared classifier — CLAUDE.md §11 COMMON):

    1. ``token_join`` (5a) — a spot ``IDX`` whose own GENUINE numeric vendor token
       (:func:`_has_genuine_numeric_token`) equals an option leg's
       ``underlying_security_id`` (:func:`_option_underlying_security_ids`). This
       makes BSE-index classification immune to symbol-spelling drift with NO
       alias and DOES change the row REF→OPT (an ``IDX`` row's pullability is the
       :func:`~brutex.universe.is_spot_only_index` rule, which keys off this
       bridged set). An NSE index carries a non-numeric token (surrogate
       ``security_id``) so it is correctly NOT token-joined.

       **EQ token-join drives PERSISTENCE, not the index bridge (DEC-FNO-EQ-TOKEN-
       JOIN-PERSIST-001, the follow-up to the DEC-FNO-INDEX-EQ-TOKEN-JOIN-AUDIT-001
       diagnostic split).** An ``EQ`` row whose GENUINE numeric token matches a leg
       is a genuinely F&O-eligible stock whose spot ``symbol_name`` ≠ its option
       leg's ``underlying_symbol`` (e.g. spot ``BAJAJ-AUTO`` vs leg ``BAJAJAUTO``).
       It is NOT added to ``options_eligible_bridged`` (that set feeds the index-only
       :func:`~brutex.universe.is_spot_only_index`, which returns ``False`` for any
       non-``IDX`` row — adding an ``EQ`` pair there would be inert). Instead its
       ``security_id`` is collected into the returned ``eq_token_join_security_ids``
       set, which :func:`~brutex.universe.is_universe_underlying` uses to PERSIST it
       as a TRADABLE (``reference_only=FALSE``) universe underlying — closing the
       prior gap where the stock was surfaced for audit only and dropped from the
       lean store. It is STILL listed in ``token_join_diagnostic`` for the operator
       audit but EXCLUDED from the reclassification ``count`` (no index flag flips).
    2. ``alias`` (5b) — a spot ``IDX`` whose ``symbol_name`` is a key in the
       sourced+override alias map (:func:`_resolve_fno_index_aliases`) whose
       ALIASED F&O symbol is options-eligible (the 2 NSE indices ``NIFTYJR`` /
       ``NIFTYMIDSELECT`` the groww_symbol bridge cannot reach).
    3. ``symbol_bridge`` — a spot ``IDX`` whose ``groww_symbol`` F&O symbol is
       options-eligible (the EXISTING :func:`_groww_symbol_index_bridge`,
       DEC-FNO-INDEX-GROWW-SYMBOL-MATCH-001).

    A row already directly in *options_eligible* (its ``symbol_name`` IS the F&O
    symbol — NIFTY / BANKNIFTY / FINNIFTY) is NOT counted as rescued; nor is the
    brute-force trio (always tradable regardless). The mechanisms are tried in the
    order above and the FIRST that fires labels the row, so the audit attributes
    each rescue to ONE mechanism deterministically.

    Returns ``(options_eligible_bridged, index_alias_symbols, rescued,
    eq_token_join_security_ids)``:

    * ``options_eligible_bridged`` — *options_eligible* UNION every rescued row's
      ``(exchange, symbol_name)``, fed to :func:`_reference_only_of`.
    * ``index_alias_symbols`` — the groww_symbol F&O aliases for EVERY ``IDX`` row
      (from :func:`_groww_symbol_index_bridge`) UNION the ALIASED F&O symbol of
      every row rescued via the alias map (e.g. ``(NSE, "NIFTYNXT50")`` for the
      ``NIFTYJR`` spot row), fed to
      :func:`~brutex.universe.diagnose_unmatched_option_underlyings` so the
      diagnostic stops reporting underlyings ANY bridge already resolved (the
      residual gap count for the 2 NSE indices drops to 0).
    * ``rescued`` — ``{mechanism: sorted [exch:symbol_name, …]}`` for the
      reconciliation audit (5c), ``mechanism`` ∈ ``token_join`` (real IDX rescues)
      / ``alias`` / ``symbol_bridge`` / ``token_join_diagnostic`` (EQ rows surfaced
      for audit — index OPT/REF classification UNCHANGED, never a real index rescue).
    * ``eq_token_join_security_ids`` — the ``SECURITY_ID``s of the symbol-mismatched
      F&O STOCKS in ``token_join_diagnostic``, fed to
      :func:`~brutex.universe.is_universe_underlying` so they are PERSISTED as
      tradable universe underlyings (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001).
    """
    bridged_pairs, symbol_alias_symbols = _groww_symbol_index_bridge(index, options_eligible)
    symbol_bridged = bridged_pairs - options_eligible  # rows the groww_symbol bridge added

    underlying_security_ids = _option_underlying_security_ids(index)
    aliases = _resolve_fno_index_aliases()

    rescued: dict[str, list[str]] = {
        "token_join": [],
        "alias": [],
        "symbol_bridge": [],
        "token_join_diagnostic": [],
    }
    rescued_pairs: set[tuple[Exchange, str]] = set()
    # SECURITY_IDs of the symbol-mismatched F&O STOCKS surfaced by the EQ token-join
    # — now PERSISTED as tradable universe underlyings (DEC-FNO-EQ-TOKEN-JOIN-PERSIST
    # -001); previously these were diagnostic-only and dropped from the lean store.
    eq_token_join_security_ids: set[int] = set()
    # Aliased F&O symbols of the rows rescued via the alias map — fed to the
    # diagnostic so the now-resolved underlying (e.g. NIFTYNXT50) stops reporting.
    alias_resolved_symbols: set[tuple[Exchange, str]] = set()
    for inst in index.universe:
        if inst.instrument not in (InstrumentType.IDX, InstrumentType.EQ):
            continue
        pair = (inst.exch_id, inst.symbol_name)
        if pair in options_eligible:
            continue  # already OPT by its own symbol — not a rescue
        aliased = (inst.exch_id, aliases.get(inst.symbol_name, ""))
        # 1. token-join (a genuine numeric vendor token matching an option leg's id).
        if _has_genuine_numeric_token(inst) and inst.security_id in underlying_security_ids:
            if inst.instrument is InstrumentType.IDX:
                # A BSE index — the index-only is_spot_only_index bridge DOES flip it
                # REF→OPT, so this is a REAL rescue (added to the bridged set).
                mechanism = "token_join"
            elif inst.exch_id == Exchange.NSE:
                # An NSE EQ stock — is_spot_only_index is False for non-IDX, so adding it
                # to the index bridged set would be inert. Instead surface it in the
                # honest token_join_diagnostic bucket AND collect its security_id into the
                # PERSIST set so is_universe_underlying keeps it as a TRADABLE underlying
                # (DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001). The NSE gate makes the set literally
                # "NSE F&O stocks": a BSE EQ cannot be an F&O stock (no BSE derivatives in
                # scope), so a coincidental BSE token collision (next arm) is excluded.
                rescued["token_join_diagnostic"].append(f"{inst.exch_id.value}:{inst.symbol_name}")
                eq_token_join_security_ids.add(inst.security_id)
                continue
            else:
                # A BSE EQ whose numeric token coincidentally equals an NSE option leg's
                # underlying_security_id — NOT an F&O stock (no BSE derivatives), so it is
                # never persisted nor surfaced. Defense-in-depth at the producer; the
                # downstream is_universe_underlying NSE gate would also reject it.
                continue
        # 2. sourced / override alias (the 2 NSE indices the symbol bridge misses).
        elif inst.instrument is InstrumentType.IDX and aliased in options_eligible:
            mechanism = "alias"
            alias_resolved_symbols.add(aliased)
        # 3. the existing groww_symbol bridge (verbose IDX whose groww F&O matches).
        elif pair in symbol_bridged:
            mechanism = "symbol_bridge"
        else:
            continue
        rescued[mechanism].append(f"{inst.exch_id.value}:{inst.symbol_name}")
        rescued_pairs.add(pair)

    options_eligible_bridged = frozenset(options_eligible | rescued_pairs)
    index_alias_symbols = symbol_alias_symbols | frozenset(alias_resolved_symbols)
    rescued = {mechanism: sorted(names) for mechanism, names in rescued.items()}
    return (
        options_eligible_bridged,
        index_alias_symbols,
        rescued,
        frozenset(eq_token_join_security_ids),
    )


def _log_opt_reclassified(rescued: dict[str, list[str]], *, still_unmatched: int) -> None:
    """Emit ONE ``universe.load.opt_reclassified`` reconciliation-audit line (5c).

    DEC-FNO-INDEX-ALIAS-001 — the operator's comprehensive OPT-vs-REF audit
    ("check all stocks/indices thoroughly"). Enumerates EVERY spot row actually
    rescued REF→OPT keyed by the mechanism that rescued it (``token_join`` (IDX) /
    ``alias`` / ``symbol_bridge``), the total ``count`` of those REAL rescues, and
    ``still_unmatched`` — the number of index-like option underlyings with NO spot
    row after ALL bridges (the residual
    :func:`~brutex.universe.diagnose_unmatched_option_underlyings` gap count, which
    is now ``0`` for the 2 NSE indices). Always emitted, even when nothing was
    rescued (a steady-state ``count=0`` is itself the audit signal); changes NO
    classification.

    The SEPARATE ``token_join_diagnostic`` field (DEC-FNO-INDEX-EQ-TOKEN-JOIN-AUDIT
    -001 — honesty) lists EQ stocks whose numeric vendor token matched an option
    leg but whose OPT/REF classification is UNCHANGED (a stock's pullability is
    ``fno_eligible`` membership, not the index-only bridge). They are surfaced for
    the operator's audit but are deliberately EXCLUDED from ``count`` — ``count`` is
    only the rows whose flag genuinely flipped, so the audit never claims a stock
    was reclassified when it was not.
    """
    reclassified = ("token_join", "alias", "symbol_bridge")
    total = sum(len(rescued[mechanism]) for mechanism in reclassified)
    _LOG.info(
        "universe.load.opt_reclassified",
        count=total,
        token_join=rescued["token_join"],
        alias=rescued["alias"],
        symbol_bridge=rescued["symbol_bridge"],
        token_join_diagnostic=rescued["token_join_diagnostic"],
        still_unmatched=still_unmatched,
    )


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
    ~750-name universe. The store's reference-id set is threaded so the summary
    ALSO carries the additive ``reference_stock_count`` / ``stock_count_total``
    split (DEC-LABEL-TRUTH-001) — the load message can then read
    "N stocks · M F&O" instead of an undifferentiated stock count.
    """
    eligible = store.fno_eligible_underlyings()
    candidates = store.list_index_equity_instruments()
    reference = store.reference_only_security_ids()
    return universe_summary(candidates, fno_eligible=eligible, reference_only_ids=reference)


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
    fno_eligible_security_ids: frozenset[int],
) -> tuple[list[Instrument], int]:
    """Select the LEAN universe to persist + count the dropped derivative legs.

    The Groww :class:`InstrumentIndex` carries every in-scope row INCLUDING the
    FUT / CE / PE legs. The lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001)
    persists ONLY the universe underlyings — every admitted IDX, every
    F&O-eligible EQ (by ticker OR by token-join ``security_id`` in
    *fno_eligible_security_ids*, DEC-FNO-EQ-TOKEN-JOIN-PERSIST-001), and every
    admitted REFERENCE_ONLY NTM constituent (an EQ whose ``security_id`` is in
    *reference_security_ids*) — and discards the legs
    (they are parsed only to discover which equities are F&O-eligible). Returns
    ``(lean_rows, legs_dropped)`` where ``legs_dropped`` is the count of in-scope
    rows NOT persisted (the legs + any non-admitted non-F&O equity), so the caller
    can report the accounting. The admission rule is delegated to the COMMON
    :func:`brutex.universe.is_universe_underlying` (CLAUDE.md §11 COMMON).
    """
    lean = [
        inst
        for inst in index.universe
        if is_universe_underlying(
            inst, fno_eligible, reference_security_ids, fno_eligible_security_ids
        )
    ]
    legs_dropped = len(index.universe) - len(lean)
    return lean, legs_dropped


def _persist_master_integrity(
    index: InstrumentIndex, store: Any, *, run_id: str, csv_sha256: str
) -> dict[str, Any]:
    """Run the parse-time structural mapping audit, log + persist it, return the summary.

    DEC-INSTRUMENT-MASTER-INTEGRITY-001 — the ONE place the leg→spot mapping can be verified
    (the lean store discards the ~89k legs straight after). Audits the FULL leg-bearing
    ``index.universe`` while it is still in memory, emits a structlog counts line, and
    persists the bounded JSON summary to ``schema_meta`` (the dashboard routes read THAT — by
    the time they run, the legs are gone). Pure audit + one INSERT; never re-stores the legs.
    """
    report = audit_master_integrity(
        index.universe,
        index_aliases=_resolve_fno_index_aliases(),
        now=datetime.now(UTC),
        run_id=run_id,
        csv_source_sha256=csv_sha256,
    )
    payload = report_to_dict(report)
    store.set_meta(_MASTER_INTEGRITY_META_KEY, json.dumps(payload))
    _LOG.info(
        "universe.load.master_integrity",
        is_green=report.is_green,
        gating=report.gating_failures,
        orphan=report.orphan_count,
        ambiguous=report.ambiguous_count,
        disagreement=report.disagreement_count,
        empty_underlying=report.empty_underlying_count,
        dedup=report.dedup_collisions,
        spots=report.type_counts.spots,
        legs=report.type_counts.legs,
        idx=report.type_counts.idx,
        eq=report.type_counts.eq,
        fut=report.type_counts.fut,
        ce=report.type_counts.ce,
        pe=report.type_counts.pe,
    )
    return payload


def _persist_economics_integrity(
    index: InstrumentIndex, store: Any, *, run_id: str, csv_sha256: str
) -> dict[str, Any]:
    """Run the parse-time economics (lot per-expiry) audit, log + persist it, return the summary.

    DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001 — the economics sibling of the mapping audit. Runs
    against the FULL leg-bearing ``index.universe`` while it is still in memory (the lean store
    discards the legs), emits a structlog counts line, and persists the bounded JSON summary to
    ``schema_meta`` (read by the dashboard economics panel + CSV). Pure audit + one INSERT.
    """
    report = audit_economics_integrity(
        index.universe,
        now=datetime.now(UTC),
        run_id=run_id,
        csv_source_sha256=csv_sha256,
    )
    payload = _economics_report_to_dict(report)
    store.set_meta(_ECONOMICS_INTEGRITY_META_KEY, json.dumps(payload))
    _LOG.info(
        "universe.load.economics_integrity",
        is_green=report.is_green,
        lot_mismatch_expiries=report.lot_mismatch_expiries,
        revisions=report.underlyings_with_lot_revisions,
        total_legs=report.total_legs,
        expiry_groups=report.expiry_groups,
    )
    return payload


def _persist_parse_skips(
    report: ParseSkipReport, store: Any, *, run_id: str, csv_sha256: str
) -> dict[str, Any]:
    """Persist the master's bounded parse-skip report, log it, return the summary.

    DEC-PARSE-SKIP-REPORT-001 — the skip sibling of the mapping / economics audits.
    The parser's per-row skip accounting (DEC-INSTRUMENT-SKIP-NO-GSYM-001) was
    previously logged then DISCARDED; this persists the bounded JSON summary to
    ``schema_meta`` (read by ``GET /api/v1/universe/parse-skips`` + the green/red
    ``/universe`` panel) so master rot is visible without grepping ``/logs``.
    GREEN iff ``skipped_count <= max(10, ceil(0.1% of in-scope rows))``
    (:func:`~brutex.adapters.groww.instruments.parse_skip_verdict`). Pure fold +
    one INSERT; the samples tuple is already capped at the parser.
    """
    payload: dict[str, Any] = {
        "skipped_count": report.skipped_count,
        "in_scope_rows": report.in_scope_rows,
        "samples": list(report.samples),
        "threshold": parse_skip_threshold(report.in_scope_rows),
        "is_green": parse_skip_verdict(report),
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "csv_source_sha256": csv_sha256,
    }
    store.set_meta(_PARSE_SKIPS_META_KEY, json.dumps(payload))
    _LOG.info(
        "universe.load.parse_skips",
        is_green=payload["is_green"],
        skipped_count=report.skipped_count,
        in_scope_rows=report.in_scope_rows,
        threshold=payload["threshold"],
    )
    return payload


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    constituent_opener: Callable[[str], str] | None = None,
    constituent_cache_dir: Path | None = None,
    ntm_revalidate_runner: Callable[[Callable[[], None]], None] | None = None,
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
    ntm_revalidate_runner
        Injectable spawner for the POST-TTL stale-while-revalidate background NTM
        cache refresh (DEC-NTM-STALE-WHILE-REVALIDATE-001). ``None`` (production)
        runs the single-flight refresh on a daemon thread; tests inject a
        synchronous / capturing runner so no real thread is ever spawned.

    Returns
    -------
    dict[str, Any]
        ``{"parsed", "persisted", "legs_dropped", "filtered", "quarantined",
        "security_id_collisions", "index_count", "stock_count", "total",
        "ntm"}`` — the load accounting
        plus the index/stock split from :func:`universe_summary` over the stored
        universe underlyings. ``filtered`` is always ``0`` (the Groww parser
        skips out-of-scope MCX / COMMODITY rows BEFORE they reach the index, so
        ``parsed`` already counts only in-scope rows) and ``quarantined`` is
        always ``0`` — kept for BACK-COMPAT only: since
        DEC-INSTRUMENT-SKIP-NO-GSYM-001 the Groww parser does NOT fail closed on
        a malformed in-scope row — it isolates, counts and skips it — and that
        skip accounting is surfaced in the NEW ``parse_skips`` key
        (DEC-PARSE-SKIP-REPORT-001: count + in-scope denominator + capped sample
        reasons + the green/red verdict, also persisted to ``schema_meta``).
        Only HEADER schema drift still fail-louds.
        ``security_id_collisions`` is the write-boundary identity-collision
        count from :class:`~brutex.data.instrument_store.UpsertResult` —
        DISTINCT-``symbol_name`` rows dropped by the store's
        ``(security_id, exch_id, segment)`` dedup (``0`` for a clean master;
        DEC-MAPPING-COLLISION-HONESTY-001). ``ntm`` is the
        NIFTY-Total-Market admission note (``{"admitted": bool, ...}`` —
        DEC-NTM-GROWW-REPOINT-001).

    Raises
    ------
    brutex.models.InstrumentFormatError
        On CSV header schema drift (the Groww 21-column header is enforced) —
        the store keeps its last good snapshot. A malformed in-scope ROW does
        NOT raise: it is skipped-and-counted
        (DEC-INSTRUMENT-SKIP-NO-GSYM-001), surfaced via ``parse_skips``.
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
        # DEC-PARSE-SKIP-REPORT-001: the report-carrying twin of ``load_index`` —
        # same SSRF check + parse + index build, but the per-row skip accounting
        # (DEC-INSTRUMENT-SKIP-NO-GSYM-001) is RETURNED instead of discarded, so
        # it can be persisted + surfaced below.
        index, parse_skip_report = load_index_with_report(lambda _url: master_text)
        parsed = len(index.universe)
        fno_eligible = build_fno_eligible_underlyings(index.universe)
        # The options-bearing underlyings (CE/PE legs) — harvested NOW, while the
        # full master still carries the legs (the lean ingest discards them). Used
        # to tag spot-only indices reference_only so the dashboard never badges a
        # non-option index "OPT" (DEC-NON-OPTION-INDEX-REFTAG-001).
        options_eligible = build_options_eligible_underlyings(index.universe)
        # Comprehensive OPT-vs-REF classification (DEC-FNO-INDEX-ALIAS-001):
        # fold the THREE bridges (vendor-canonical token-join for numeric-token
        # stocks + BSE indices; sourced/override alias for the 2 NSE indices
        # NIFTYJR / NIFTYMIDSELECT; the existing groww_symbol bridge) into one
        # bridged options-eligible set so no genuine F&O underlying is silently
        # dropped to REFERENCE_ONLY by symbol-spelling drift. ``rescued`` is the
        # REF→OPT audit (5c); ``index_alias_symbols`` suppresses every resolved
        # underlying from the residual gap diagnostic.
        options_eligible_bridged, index_alias_symbols, rescued, fno_eligible_security_ids = (
            _classify_option_pullable(index, options_eligible)
        )

        # OBSERVE-ONLY diagnostic (DEC-FNO-INDEX-MATCH-DIAG-001): surface any
        # NSE option underlying that LOOKS like an index but matches no IDX
        # spot row even AFTER ALL bridges — a residual gap none could resolve
        # (logged with the real strings, changes no classification). Resolved
        # indices are passed as index_alias_symbols so they are no longer
        # reported (honest count → 0 for the 2 NSE indices).
        residual_gaps = diagnose_unmatched_option_underlyings(index.universe, index_alias_symbols)
        _log_fno_index_gaps(residual_gaps)
        # Reconciliation audit (DEC-FNO-INDEX-ALIAS-001, 5c): one line enumerating
        # every spot row rescued REF→OPT by which mechanism, plus the residual
        # still-unmatched count — the operator's "check all stocks/indices" audit.
        _log_opt_reclassified(rescued, still_unmatched=len(residual_gaps))

        # NTM enrichment is fail-soft — it never blocks the keyless core load
        # (DEC-NTM-GROWW-REPOINT-001). The returned ids ADMIT the non-F&O
        # constituents into the lean set AND tag them reference_only at upsert.
        reference_security_ids, ntm_note = _ntm_admission(
            index,
            fno_eligible=fno_eligible,
            fno_eligible_security_ids=fno_eligible_security_ids,
            constituent_opener=constituent_opener,
            cache_dir=(
                constituent_cache_dir if constituent_cache_dir is not None else default_cache_dir()
            ),
            revalidate_runner=ntm_revalidate_runner,
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
            index,
            fno_eligible=fno_eligible,
            reference_security_ids=reference_security_ids,
            fno_eligible_security_ids=fno_eligible_security_ids,
        )
        # Stamp each underlying with its TRADABLE F&O lot + freeze from its legs
        # (DEC-GROWW-LEG-ECONOMICS-001) — the legs carry the real economics; the
        # IDX / EQ row carries only its cash placeholder. This also keeps
        # ``has_placeholder_stock_economics`` False so the auto-heal does not loop.
        lean = _fold_leg_economics(lean, build_underlying_economics(index.universe))
        # Capture the UpsertResult so the write-boundary identity-collision count
        # (DEC-MAPPING-COLLISION-HONESTY-001) rides the load report — the store
        # already WARNs per drop; this surfaces the aggregate to the operator.
        upsert = store.upsert_many(
            lean,
            snapshot_date=date.today(),
            run_id=run_id,
            csv_source_sha256=csv_sha256,
            replace_snapshot=True,
            reference_only_of=_reference_only_of(reference_security_ids, options_eligible_bridged),
        )
        pruned = store.prune_to_latest_snapshot()
        # Parse-time structural mapping audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001): verify
        # every FUT/CE/PE leg maps to exactly ONE IDX/EQ spot (symbol AND token). Runs against
        # the still-in-memory full leg-bearing ``index.universe`` (``_lean_universe`` returns a
        # NEW list, it never mutates ``index``), but persists AFTER the lean upsert + prune so
        # the schema_meta snapshot is transactionally consistent with the rows that persisted
        # (it can never describe a master whose upsert rolled back). Persists the bounded JSON
        # summary to schema_meta (read by the dashboard panel + CSV) + a structlog counts line.
        master_integrity = _persist_master_integrity(
            index, store, run_id=run_id, csv_sha256=csv_sha256
        )
        # Economics audit (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001): lot_size uniform per
        # (underlying, expiry); same full-leg-bearing universe, same fail-safe pattern.
        economics_integrity = _persist_economics_integrity(
            index, store, run_id=run_id, csv_sha256=csv_sha256
        )
        # Parse-skip honesty (DEC-PARSE-SKIP-REPORT-001): persist the parser's bounded
        # skip report (count + capped reasons + green/red verdict) — same fail-safe
        # persist-after-upsert ordering as the two audits above.
        parse_skips = _persist_parse_skips(
            parse_skip_report, store, run_id=run_id, csv_sha256=csv_sha256
        )
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
        security_id_collisions=upsert.security_id_collisions,
    )
    return {
        "parsed": parsed,
        "persisted": len(lean),
        "legs_dropped": legs_dropped,
        # The Groww parser skips out-of-scope rows BEFORE the index, so they
        # never reach this layer — `parsed` already counts only in-scope rows.
        "filtered": 0,
        # BACK-COMPAT stub: since DEC-INSTRUMENT-SKIP-NO-GSYM-001 the parser
        # skips-and-counts a malformed in-scope row (it does NOT fail closed);
        # the honest accounting lives in "parse_skips" below — this key is kept
        # 0 so no existing consumer breaks (DEC-PARSE-SKIP-REPORT-001).
        "quarantined": 0,
        # Write-boundary identity collisions (DEC-MAPPING-COLLISION-HONESTY-001):
        # rows the store's (security_id, exch_id, segment) dedup DROPPED because a
        # DISTINCT symbol_name shared the identity — the observed "13, NSE, CASH"
        # case. 0 for a clean master; each drop is also logged at WARNING by the
        # store naming BOTH symbols.
        "security_id_collisions": upsert.security_id_collisions,
        "index_count": summary["index_count"],
        "stock_count": summary["stock_count"],
        "total": summary["total"],
        # ADDITIVE label-truth split (DEC-LABEL-TRUTH-001): the Stocks figure
        # above counts the FULL held universe on the lean store (reference rows
        # included — eligible-by-storage, see _summarise_stored); these two keys
        # let the /universe load message honestly label the F&O subset beside it
        # ("N stocks · M F&O", M = stock_count_total − reference_stock_count).
        "reference_stock_count": summary["reference_stock_count"],
        "stock_count_total": summary["stock_count_total"],
        # NTM admission status (DEC-NTM-GROWW-REPOINT-001) — surfaced so the
        # operator SEES whether NIFTY Total Market's constituents were admitted
        # this load (and, if skipped, the honest reason).
        "ntm": ntm_note,
        # Instrument-master structural integrity audit (DEC-INSTRUMENT-MASTER-INTEGRITY-001)
        # — the green/red leg↔spot mapping verdict, also persisted to schema_meta.
        "master_integrity": master_integrity,
        # Economics (lot-per-expiry) integrity audit (DEC-INSTRUMENT-ECONOMICS-INTEGRITY-001).
        "economics_integrity": economics_integrity,
        # Parse-skip honesty report (DEC-PARSE-SKIP-REPORT-001) — the malformed
        # in-scope rows the parser skipped-and-counted, with the green/red verdict.
        "parse_skips": parse_skips,
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
