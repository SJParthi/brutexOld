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

import threading
from collections.abc import Callable
from datetime import date
from typing import Any

from brutex.adapters.dhan.instrument_loader import urllib_http_get
from brutex.adapters.dhan.instrument_refresh import refresh_instruments
from brutex.adapters.dhan.instruments import DhanInstrumentError
from brutex.dashboard.universe_query import universe_summary
from brutex.models import InstrumentFormatError

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


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
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

    Returns
    -------
    dict[str, Any]
        ``{"parsed", "persisted", "legs_dropped", "filtered", "quarantined",
        "index_count", "stock_count", "total"}`` — the refresh accounting
        (``quarantined`` is always ``0``: Dhan **fails closed** on a malformed
        universe row rather than quarantining) plus the index/stock split from
        :func:`universe_summary` over the stored universe underlyings.

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
        # refresh_instruments downloads → selects the universe → full-snapshot
        # upserts → prunes to the newest snapshot at the single chokepoint, so
        # the lake stays lean (~241 rows) without each caller remembering to
        # prune (DEC-INSTRUMENT-UNIVERSE-FAST-001).
        try:
            result = refresh_instruments(store, fetch, snapshot_date=date.today(), run_id=run_id)
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
    }


__all__ = ["load_instrument_master"]
