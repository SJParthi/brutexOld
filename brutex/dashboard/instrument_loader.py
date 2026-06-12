"""Keyless instrument-master load for the ``/universe`` dashboard (Groww).

The ``/universe`` page is empty until SOMEONE has populated the instrument
master — the list of NSE/BSE indices + F&O-eligible single stocks. That master
is a **public CSV** (the Groww instrument master,
``https://growwapi-assets.groww.in/instruments/instrument.csv``) that needs
**NO Groww API token** — only candles / options need authentication, and those
are a separate, later concern (DEC-VENDOR-PIVOT-001).

PR-J4b vendor cutover (DEC-DHAN-GROWW-REMIGRATE-001, ADR-0009 §5): this module
was repointed from the Dhan 31-column detailed scrip master to the Groww
21-column instrument master — the J2-equivalent the dashboard never got. The
Groww CSV schema differs, so the parse goes through the Groww parser
(:func:`~brutex.adapters.groww.instrument_loader.load_index` →
:class:`~brutex.adapters.groww.instruments.InstrumentIndex`), NOT the Dhan one.

This module is the thin, fully-testable seam that:

1. downloads + parses the Groww instrument master into an in-memory
   :class:`~brutex.adapters.groww.instruments.InstrumentIndex` (the SSRF-guarded,
   **no-auth** public-CSV path), then
2. derives the F&O-eligible equity set from the parsed derivative legs and
   **full-snapshot upserts the LEAN universe** (every admitted IDX + every
   F&O-eligible EQ; the ~tens-of-thousands of FUT / CE / PE legs are parsed for
   eligibility then DISCARDED per DEC-INSTRUMENT-UNIVERSE-FAST-001) into the
   vendor-neutral :class:`~brutex.data.instrument_store.InstrumentStore`, then
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

## NTM (NIFTY Total Market) enrichment — gated OFF on Groww

The Dhan loader auto-admitted NIFTY Total Market + its ~750 constituents
(DEC-NIFTY-TOTAL-MARKET-LOAD-001) via the Dhan-specific
``brutex.adapters.dhan.ntm`` discovery. That discovery has **no Groww twin**
yet, so — per Golden Rule #1 (no invented vendor behaviour) — NTM enrichment is
a clean **no-op** here: the returned ``ntm`` note is always
``{"admitted": False, "reason": "<NTM discovery is Dhan-only; …>"}``. NTM can
return in a later PR once a Groww discovery path exists.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import date
from typing import Any

import structlog

from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get
from brutex.adapters.groww.instruments import InstrumentIndex
from brutex.dashboard.universe_query import universe_summary
from brutex.models import Instrument
from brutex.universe import build_fno_eligible_underlyings, is_universe_underlying

_LOG = structlog.get_logger(__name__)

#: NTM (NIFTY Total Market) is NOT auto-admitted on Groww — the Dhan discovery
#: (``brutex.adapters.dhan.ntm``) has no Groww twin yet, and we do not invent
#: vendor behaviour (Golden Rule #1). The keyless load returns this fixed note so
#: the operator SEES that NTM was deliberately skipped (not silently dropped).
_NTM_SKIPPED_NOTE: dict[str, Any] = {
    "admitted": False,
    "reason": (
        "NIFTY Total Market auto-admission is Dhan-only — its discovery has no "
        "Groww equivalent yet (Golden Rule #1, no invented vendor behaviour); it "
        "can return in a later PR"
    ),
}

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


def _lean_universe(index: InstrumentIndex) -> tuple[list[Instrument], int]:
    """Select the LEAN universe to persist + count the dropped derivative legs.

    The Groww :class:`InstrumentIndex` carries every in-scope row INCLUDING the
    FUT / CE / PE legs. The lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001)
    persists ONLY the universe underlyings — every admitted IDX + every
    F&O-eligible EQ — and discards the legs (they are parsed only to discover
    which equities are F&O-eligible). Returns ``(lean_rows, legs_dropped)`` where
    ``legs_dropped`` is the count of in-scope rows NOT persisted (the legs +
    any non-F&O equity), so the caller can report the accounting like the Dhan
    refresh did. The F&O-eligibility rule is delegated to the COMMON
    :func:`brutex.universe.build_fno_eligible_underlyings` /
    :func:`brutex.universe.is_universe_underlying` (CLAUDE.md §11 COMMON).
    """
    fno_eligible = build_fno_eligible_underlyings(index.universe)
    lean = [inst for inst in index.universe if is_universe_underlying(inst, fno_eligible)]
    legs_dropped = len(index.universe) - len(lean)
    return lean, legs_dropped


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Keylessly download + parse the Groww instrument master into *store*.

    Downloads the public Groww instrument-master CSV (NO Groww token), parses it
    into the O(1) :class:`~brutex.adapters.groww.instruments.InstrumentIndex`,
    full-snapshot upserts the LEAN universe (IDX + F&O-eligible EQ) into *store*,
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
        row — Golden Rule #1 — rather than quarantining). ``ntm`` is the fixed
        :data:`_NTM_SKIPPED_NOTE` (NTM auto-admission is Dhan-only).

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

        # Lean ingest (DEC-INSTRUMENT-UNIVERSE-FAST-001): persist ONLY the
        # universe underlyings (IDX + F&O-eligible EQ); the FUT / CE / PE legs are
        # parsed for eligibility then dropped. Full-snapshot replace so the day's
        # rows are EXACTLY the lean set (idempotent on re-pull).
        lean, legs_dropped = _lean_universe(index)
        store.upsert_many(
            lean,
            snapshot_date=date.today(),
            run_id=run_id,
            csv_source_sha256=csv_sha256,
            replace_snapshot=True,
        )
        pruned = store.prune_to_latest_snapshot()

        # Lean-store-correct summary (DEC-INSTRUMENT-UNIVERSE-FAST-001): the store
        # now holds ONLY the universe underlyings (no option/future legs), so
        # summarise from the IDX + EQ candidates plus the SQL-derived eligible set
        # — the COMMON ``universe_summary`` classifier.
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        summary = universe_summary(candidates, fno_eligible=eligible)

    _LOG.info(
        "universe.load.groww",
        run_id=run_id,
        parsed=parsed,
        persisted=len(lean),
        legs_dropped=legs_dropped,
        pruned=pruned,
        index_count=summary["index_count"],
        stock_count=summary["stock_count"],
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
        # NTM auto-admission is Dhan-only (Golden Rule #1) — a fixed skipped note.
        "ntm": dict(_NTM_SKIPPED_NOTE),
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
