"""Keyless instrument-master load for the ``/universe`` dashboard.

The ``/universe`` page is empty until SOMEONE has populated the instrument
master — the list of NSE/BSE indices + F&O-eligible single stocks. That master
is a **public CSV** (the Dhan detailed scrip master,
``docs/dhan-ref/09``) that needs **NO Dhan API token** — only candles /
options need authentication, and those are a separate, later concern
(DEC-VENDOR-PIVOT-001).

This module is the thin, fully-testable seam that:

1. downloads + parses + full-snapshot-upserts the Dhan scrip master into the
   store via :func:`~brutex.adapters.dhan.instrument_refresh.refresh_instruments`
   (the SSRF-guarded, **no-auth** public-CSV path), then
2. reads the snapshot back and summarises it with the COMMON
   :func:`brutex.dashboard.universe_query.universe_summary` classifier
   (no re-derivation of the index-vs-stock split).

There is deliberately **no** auth reference anywhere in this path — populating
the LIST is keyless by design.

The live HTTP fetch (:func:`~brutex.adapters.dhan.instrument_loader.urllib_http_get`)
is ``# pragma: no cover`` because CI is network-blocked; it is reached on the
operator's Mac. Tests inject a fake ``http_get`` returning a small canned CSV
against a REAL temp :class:`~brutex.data.instrument_store.InstrumentStore`, so
every non-pragma line + branch in this module is covered offline.
"""

from __future__ import annotations

import csv
import io
import threading
from collections.abc import Callable
from datetime import date
from typing import Any

import structlog

from brutex.adapters.dhan.constants import INSTRUMENT_MASTER_DETAILED_URL
from brutex.adapters.dhan.instrument_loader import urllib_http_get
from brutex.adapters.dhan.instrument_refresh import refresh_instruments
from brutex.adapters.dhan.instruments import DhanInstrumentError, IndexDiscoveryError
from brutex.adapters.dhan.ntm import (
    NTM_NAME_PATTERN,
    prepare_ntm_reference_plan,
    resolve_ntm_index,
)
from brutex.dashboard.nse_constituents import fetch_constituent_csv
from brutex.dashboard.universe_query import universe_summary
from brutex.models import InstrumentFormatError

_LOG = structlog.get_logger(__name__)

#: The index NAME whose constituent CSV is fetched for NTM enrichment — the key
#: ``constituent_url`` maps to the operator-verified niftyindices URL
#: (DEC-NIFTY-TOTAL-MARKET-URL-001).
_NTM_INDEX_NAME = "NIFTY TOTAL MARKET"

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


def _ntm_admission(
    master_text: str,
    *,
    constituent_opener: Callable[[str], str] | None,
) -> tuple[frozenset[str], frozenset[int], dict[str, Any]]:
    """Best-effort NTM admission args from the already-fetched master (+ its CSV).

    Wires NIFTY Total Market into the keyless load (DEC-NIFTY-TOTAL-MARKET-LOAD-001):
    discover NTM's ``INDEX`` row in THIS master (no network), then — only if found
    — fetch its constituent CSV and resolve the ~755 members to ``SECURITY_ID``s by
    ISIN. Returns ``(extra_index_symbols, reference_security_ids, note)`` to pass to
    :func:`refresh_instruments` so NTM is admitted as a REFERENCE_ONLY index and its
    constituents as REFERENCE_ONLY stocks.

    **Fail-soft:** any failure (NTM absent from the master, constituent host
    unreachable, parse/coverage issue) returns empty admission args + a ``note``
    explaining why — a network hiccup NEVER breaks the keyless core load of the
    ~241 universe underlyings. ``require_full_coverage=False`` + pattern-only
    discovery (``expected_security_id=None``) maximise the chance NTM actually
    appears; the discovered id is reported in ``note`` for the operator to confirm
    against the Dhan-web-verified ``46``.
    """
    note: dict[str, Any] = {"admitted": False}
    reader = csv.DictReader(io.StringIO(master_text), restval="")
    fieldnames = reader.fieldnames or []
    rows = [{name: (row.get(name) or "") for name in fieldnames} for row in reader]

    # 1) Discover NTM's INDEX row in THIS master FIRST — NO network. Absent (or
    #    unusable) → skip cleanly (the common case for a non-NTM master, e.g. a
    #    test fixture), so we never reach the constituent fetch. Pattern-only
    #    (expected_security_id=None): admit the REAL row this master carries; its
    #    id is reported for the operator to confirm against the web-verified 46.
    try:
        index = resolve_ntm_index(rows, expected_security_id=None)
    except (IndexDiscoveryError, DhanInstrumentError) as exc:
        note["error"] = str(exc)
        return frozenset(), frozenset(), note

    extra_index_symbols = frozenset({index.symbol_name.upper()})
    reference_security_ids: set[int] = {index.security_id}  # tag the index reference_only too
    note.update(
        admitted=True,
        index_symbol=index.symbol_name,
        index_security_id=index.security_id,
        constituents=0,
    )

    # 2) NTM IS in the master — now fetch + resolve its constituents (network,
    #    best-effort). A fetch/parse failure degrades to INDEX-ONLY admission (NTM
    #    still appears as an index) with the reason noted — never breaks the load.
    try:
        csv_text = fetch_constituent_csv(_NTM_INDEX_NAME, opener=constituent_opener)
        if csv_text:
            plan = prepare_ntm_reference_plan(
                rows,
                csv_text,
                pattern=NTM_NAME_PATTERN,
                expected_security_id=None,
                require_full_coverage=False,
            )
            reference_security_ids |= set(plan.reference_security_ids)
            note["constituents"] = plan.constituent_count
        else:
            note["constituent_note"] = "no constituent CSV mapping for NIFTY TOTAL MARKET"
    except (OSError, DhanInstrumentError, IndexDiscoveryError) as exc:
        # URLError/HTTPError/timeout (OSError) or a parse/discovery hiccup — keep
        # the index, drop the constituents this load, surface why.
        note["constituent_note"] = f"constituent resolution failed: {exc}"

    return extra_index_symbols, frozenset(reference_security_ids), note


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
    constituent_opener: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Keylessly download + parse the Dhan instrument master into *store*.

    Calls :func:`refresh_instruments` (the public CSV — NO Dhan token), then
    reads the resulting snapshot back from *store* and returns headline counts
    for the ``/universe`` page.

    Parameters
    ----------
    store
        A :class:`~brutex.data.instrument_store.InstrumentStore` (or a
        duck-typed sink exposing ``upsert_many`` + ``prune_to_latest_snapshot``).
    http_get
        Injected ``url -> CSV text`` for offline tests. When ``None`` (the
        production path), the real SSRF-guarded, keyless public-CSV fetch
        (:func:`urllib_http_get`, restricted to ``images.dhan.co``) is used.
    run_id_factory
        Optional ``() -> run_id`` stamped on the upsert for audit lineage.
    constituent_opener
        Injected ``url -> CSV text`` for the NTM constituent fetch (offline
        tests). When ``None`` (production) the real best-effort niftyindices
        opener is used. NTM enrichment runs only AFTER NTM's index row is found
        in the master, so a master without NTM never touches the network here.

    Returns
    -------
    dict[str, Any]
        ``{"parsed", "persisted", "legs_dropped", "filtered", "quarantined",
        "index_count", "stock_count", "total", "ntm"}`` — the refresh accounting
        (``quarantined`` is always ``0``: Dhan **fails closed** on a malformed
        universe row rather than quarantining) plus the index/stock split from
        :func:`universe_summary` over the stored universe underlyings. ``ntm`` is
        the NIFTY-Total-Market admission status
        (``{"admitted": bool, ...}`` — DEC-NIFTY-TOTAL-MARKET-LOAD-001).

    Raises
    ------
    brutex.models.InstrumentFormatError
        On CSV header schema drift (a required column is missing) or any
        malformed universe row — the store keeps its last good snapshot.
    """
    fetch = http_get
    if fetch is None:
        fetch = urllib_http_get  # pragma: no cover — SSRF-guarded network path
    run_id = (run_id_factory or (lambda: "instruments-load"))()

    # Serialise the whole refresh + read-back under the module lock so two
    # concurrent loads cannot interleave a snapshot wipe with another's insert
    # (DEC-UNDERLYING-ECONOMICS-001 — the "25 indices · 0 stocks" corruption).
    with _LOAD_LOCK:
        # Fetch the master ONCE, then reuse the text for both NTM discovery and
        # the refresh (no double download). NTM enrichment is fail-soft — it never
        # blocks the keyless core load (DEC-NIFTY-TOTAL-MARKET-LOAD-001).
        master_text = fetch(INSTRUMENT_MASTER_DETAILED_URL)
        extra_index_symbols, reference_security_ids, ntm_note = _ntm_admission(
            master_text, constituent_opener=constituent_opener
        )
        if ntm_note.get("admitted"):
            _LOG.info("universe.load.ntm_admitted", **ntm_note)
        else:
            _LOG.info("universe.load.ntm_skipped", **ntm_note)

        # refresh_instruments downloads → selects the universe → full-snapshot
        # upserts → prunes to the newest snapshot at the single chokepoint, so
        # the lake stays lean without each caller remembering to prune
        # (DEC-INSTRUMENT-UNIVERSE-FAST-001). We feed back the cached master text
        # + the NTM admission args (empty by default → byte-identical).
        try:
            result = refresh_instruments(
                store,
                lambda _url: master_text,
                snapshot_date=date.today(),
                run_id=run_id,
                reference_security_ids=reference_security_ids,
                extra_index_symbols=extra_index_symbols,
            )
        except DhanInstrumentError as exc:
            # Translate to the dashboard's stable contract so app.py's existing
            # 502 handler (InstrumentFormatError, OSError) is unchanged.
            raise InstrumentFormatError(str(exc)) from exc

        # Lean-store-correct summary (DEC-INSTRUMENT-UNIVERSE-FAST-001): the
        # store now holds ONLY the universe underlyings (no option/future
        # legs), so summarise from the IDX + EQ candidates plus the SQL-derived
        # eligible set — the COMMON ``universe_summary`` classifier.
        eligible = store.fno_eligible_underlyings()
        candidates = store.list_index_equity_instruments()
        summary = universe_summary(candidates, fno_eligible=eligible)

    return {
        "parsed": result.parsed,
        "persisted": result.persisted,
        "legs_dropped": result.legs_dropped,
        "filtered": result.filtered,
        "quarantined": 0,  # Dhan fails closed on a bad row — no quarantine bucket
        "index_count": summary["index_count"],
        "stock_count": summary["stock_count"],
        "total": summary["total"],
        # NTM enrichment status (DEC-NIFTY-TOTAL-MARKET-LOAD-001) — surfaced so the
        # operator SEES whether NIFTY Total Market + its constituents were admitted
        # this load (and, if skipped, the honest reason).
        "ntm": ntm_note,
    }


__all__ = ["load_instrument_master"]
