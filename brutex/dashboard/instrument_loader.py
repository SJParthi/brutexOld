"""Keyless instrument-master load for the ``/universe`` dashboard.

The ``/universe`` page is empty until SOMEONE has populated the instrument
master — the list of NSE indices + F&O-eligible single stocks. That master
is a **public CSV** (``growwapi-assets.groww.in/instruments/instrument.csv``,
CLAUDE.md §6) that needs **NO Groww API keys** — only expiries / contracts /
candles need authentication, and those are a separate, later concern.

This module is the thin, fully-testable seam that:

1. constructs an :class:`~brutex.adapters.groww.instruments.InstrumentMasterFetcher`
   — the existing SSRF-guarded, **no-auth** public-CSV fetcher,
2. calls :meth:`~brutex.adapters.groww.instruments.InstrumentMasterFetcher.refresh`
   to download + parse + upsert the master into the store, then
3. reads the snapshot back and summarises it with the COMMON
   :func:`brutex.dashboard.universe_query.universe_summary` classifier
   (no re-derivation of the index-vs-stock split).

There is deliberately **no** :class:`~brutex.adapters.groww.auth.GrowwAuth`
reference anywhere in this path — populating the LIST is keyless by design.

The live HTTP fetch (:meth:`InstrumentMasterFetcher.from_env`) is
``# pragma: no cover`` because CI is network-blocked; it is reached on the
operator's Mac. Tests inject a fake ``http_get`` returning a small canned CSV
against a REAL temp :class:`~brutex.data.instrument_store.InstrumentStore`, so
every non-pragma line + branch in this module is covered offline.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from brutex.adapters.groww.instruments import InstrumentMasterFetcher
from brutex.dashboard.universe_query import universe_summary


def load_instrument_master(
    store: Any,
    *,
    http_get: Callable[[str], str] | None = None,
    run_id_factory: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Keylessly download + parse the Groww instrument master into *store*.

    Constructs an :class:`InstrumentMasterFetcher`, refreshes it (the public
    CSV — NO Groww keys), then reads the resulting snapshot back from *store*
    and returns headline counts for the ``/universe`` page.

    Parameters
    ----------
    store
        A :class:`~brutex.data.instrument_store.InstrumentStore` (or a
        duck-typed sink exposing ``upsert_many`` + ``load_snapshot``). The
        parsed instruments are upserted here and then re-read.
    http_get
        Injected ``url -> CSV text`` for offline tests. When ``None`` (the
        production path), the real SSRF-guarded, keyless public-CSV fetch is
        wired via :meth:`InstrumentMasterFetcher.from_env`.
    run_id_factory
        Optional ``() -> run_id`` stamped on the upsert for audit lineage.
        Used only on the injected (test) path; the ``from_env`` path supplies
        its own uuid-based factory.

    Returns
    -------
    dict[str, Any]
        ``{"parsed", "persisted", "legs_dropped", "filtered", "quarantined",
        "index_count", "stock_count", "total"}`` where ``parsed`` /
        ``persisted`` / ``legs_dropped`` / ``filtered`` / ``quarantined``
        come from the refresh result and ``index_count`` / ``stock_count`` /
        ``total`` come from summarising the stored universe underlyings via
        :func:`universe_summary` (the fast IDX + EQ candidate path).

    Raises
    ------
    brutex.models.InstrumentFormatError
        On CSV header schema drift (a required column is missing) — the
        store keeps its last good snapshot.
    """
    if http_get is None:
        fetcher = InstrumentMasterFetcher.from_env(store)  # pragma: no cover
    else:
        fetcher = InstrumentMasterFetcher(
            http_get=http_get,
            store=store,
            run_id_factory=run_id_factory or (lambda: "instruments-load"),
        )

    result = fetcher.refresh()

    # Fast + lean-store-correct path (DEC-INSTRUMENT-UNIVERSE-FAST-001): the
    # store now holds ONLY the universe underlyings (no option/future legs),
    # so summarise from the IDX + EQ candidates plus the SQL-derived eligible
    # set rather than a full snapshot (which would carry no legs to infer
    # eligibility from). Both paths use the COMMON ``universe_summary``
    # classifier — no re-derivation of the index-vs-stock split.
    eligible = store.fno_eligible_underlyings()
    candidates = store.list_index_equity_instruments()
    summary = universe_summary(candidates, fno_eligible=eligible)

    return {
        "parsed": result.parsed,
        "persisted": result.persisted,
        "legs_dropped": result.legs_dropped,
        "filtered": result.filtered,
        "quarantined": result.quarantined,
        "index_count": summary["index_count"],
        "stock_count": summary["stock_count"],
        "total": summary["total"],
    }


__all__ = ["load_instrument_master"]
