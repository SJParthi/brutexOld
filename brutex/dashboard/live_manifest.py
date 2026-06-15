"""Build the live-capture subscription manifest from the PERSISTED tables.

The N-connection capture (``python -m brutex.live capture-all --manifest <json>``)
reads a plain ``{"spot": [...], "legs": [...]}`` JSON of feed-row dicts; THIS
module is the dashboard half that WRITES it from the lake the dashboard already
owns:

* ``spot`` — the in-scope ``instruments`` rows that are subscribable spot
  (``instrument`` in ``IDX`` / ``EQ``): the 25 admitted indices on the
  index-value feed + the ~750 cash equities on the ltp feed — the exact 775 the
  ``/db`` instruments table holds.
* ``legs`` — the F&O ``contracts`` registry legs (``CE`` / ``PE`` / ``FUT``) on
  the ``FNO`` segment. When the registry cache is absent / empty, ``legs`` is
  ``[]`` — degrade honestly, never invent rows (Golden Rule #1).

Decoupling (``DEC-LIVE-DECOUPLE-001``): this module reads the instrument store +
the F&O registry (the data plane) and emits a PLAIN JSON file — it NEVER imports
:mod:`brutex.live`. The capture subprocess is the only thing that imports the live
package; the manifest is the file contract between them, so ``brutex/live/`` stays
imported-by-nothing and portable to TickVault / AWS. Read-only market data, no
orders — building a manifest issues only ``SELECT`` reads.

Each emitted feed-row carries the four keys the capture's
``read_subscription_manifest`` validates — ``exchange`` / ``segment`` /
``exchange_token`` / ``instrument_type`` — plus an optional ``underlying_symbol``
so a spot connection can dual-feed-route each instrument.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.dashboard.db_view import registry_path_for
from brutex.data.registry_store import RegistryStore

_LOG = structlog.get_logger("brutex.dashboard.live_manifest")

#: Fallback registry filename when the store has no resolvable on-disk path — the
#: file is absent in that case, so :func:`build_leg_rows` degrades to ``legs=[]``.
_FALLBACK_REGISTRY_NAME: Final[str] = "registry.duckdb"

#: The ``instrument`` types that are SUBSCRIBABLE spot: indices (index-value feed)
#: + cash equities (ltp feed). FUT/CE/PE are NOT spot — they are the legs.
_SPOT_INSTRUMENT_TYPES: Final[frozenset[str]] = frozenset({"IDX", "EQ"})

#: The vendor segment every F&O leg rides (the capture shards legs onto the ltp
#: leg connections). The registry does not persist a segment column, so it is
#: stamped here from the single F&O segment the legs belong to.
_LEG_SEGMENT: Final[str] = "FNO"

#: A resolver ``contract_symbol -> numeric exchange_token string`` (or ``None``
#: when the symbol is unknown / expired). The numeric token is the value
#: ``subscribe_ltp`` requires for an F&O leg ("as provided in instrument csv" —
#: ``docs/groww/api-reference/07-feed.md:54``), and it is NOT persisted in the
#: lean master / registry, so a leg's feed token is RESOLVED at manifest build.
FeedTokenResolver = Callable[[str], "str | None"]


def _no_feed_token(_symbol: str) -> None:
    """Default :data:`FeedTokenResolver` — resolve nothing (every leg skipped).

    When no production resolver is wired the manifest degrades HONESTLY: legs
    are skipped rather than emitting a guessed / wrong token (Golden Rule #1).
    Returns ``None`` implicitly for every symbol.
    """


#: The segment EVERY spot row rides on the Groww live feed. Both
#: ``subscribe_index_value`` (indices) AND ``subscribe_ltp`` (cash equities) take
#: ``segment="CASH"`` — ``docs/groww/api-reference/07-feed.md:54,132``. This is
#: NOT the persisted ``Segment`` (indices are stored as the internal ``"INDEX"``
#: for PK disambiguation, DEC-INDEX-SEGMENT-PK-FIX-001); the feed needs ``CASH``.
_SPOT_FEED_SEGMENT: Final[str] = "CASH"


def _spot_row(instrument: Any) -> dict[str, Any]:
    """Project one :class:`~brutex.models.Instrument` to a Groww FEED-row dict.

    Emits the EXACT shape ``subscribe_index_value`` / ``subscribe_ltp`` expect
    (``07-feed.md:54,132``), NOT the persisted engine identifiers:

    * ``segment`` = ``"CASH"`` (:data:`_SPOT_FEED_SEGMENT`) — the feed segment for
      BOTH indices and equities, never the internal ``"INDEX"`` segment;
    * ``exchange_token`` = the RAW Groww feed token (``instrument.feed_token``:
      ``"NIFTY"`` / ``"1"`` / ``"2885"``), NOT ``security_id`` (which hashes a
      non-numeric index token into a surrogate). Legacy rows with no
      ``feed_token`` fall back to ``security_id`` (correct for numeric equities;
      a string-token index without ``feed_token`` is a pre-v7 store that
      re-loads on the next ``make run``).

    Plus ``instrument_type`` (for the dual-feed routing) and the optional
    ``underlying_symbol``.
    """
    token = instrument.feed_token if instrument.feed_token is not None else instrument.security_id
    return {
        "exchange": str(instrument.exch_id),
        "segment": _SPOT_FEED_SEGMENT,
        "exchange_token": str(token),
        "instrument_type": str(instrument.instrument),
        "underlying_symbol": instrument.underlying_symbol,
    }


def build_spot_rows(store: Any) -> list[dict[str, Any]]:
    """Return the FULL spot feed-rows (the 775) from the instrument store.

    Reads ``store.list_index_equity_instruments()`` — every latest-snapshot
    ``IDX`` + ``EQ`` row (the 25 admitted indices + the ~750 cash equities = the
    exact 775 the ``/db`` instruments table holds), NOT ``list_in_scope`` (which
    is the ~4 brute-force-tradable rows only). The live feed is REFERENCE/RESEARCH
    so it captures the whole spot universe, not just the engine's tradable scope.
    FUT / CE / PE legs (if any survived the lean master) are not returned here —
    they are the manifest's ``legs``, sourced from the registry. Read-only.

    Parameters
    ----------
    store
        The wired :class:`~brutex.data.instrument_store.InstrumentStore` (its
        ``list_index_equity_instruments`` is the full IDX+EQ latest-snapshot
        reader, already used by the ``/universe`` explorer).

    Returns
    -------
    list[dict[str, Any]]
        One Groww feed-row per spot instrument, in the store's
        ``list_index_equity_instruments`` order (stable for an idempotent manifest).
    """
    rows: list[dict[str, Any]] = []
    for instrument in store.list_index_equity_instruments():
        if str(instrument.instrument) in _SPOT_INSTRUMENT_TYPES:
            rows.append(_spot_row(instrument))
    return rows


def _leg_row(contract: Any, feed_token: str) -> dict[str, Any]:
    """Project one registry :class:`~brutex.data.registry_store.ContractRow` to a leg feed-row.

    The registry persists the addressing identity ``(exch, contract_symbol,
    side)`` but no numeric token / segment, so the feed-row stamps:

    * ``exchange`` = the contract's ``exch``;
    * ``segment`` = :data:`_LEG_SEGMENT` (every leg is F&O);
    * ``exchange_token`` = *feed_token*, the NUMERIC instrument-master token
      ``subscribe_ltp`` requires ("as provided in instrument csv" —
      ``docs/groww/api-reference/07-feed.md:54``; CLAUDE.md §6); the
      ``contract_symbol`` addressing STRING (e.g. ``NSE-NIFTY-26Jun26-23000-CE``)
      is the wrong value here — it is not a token, so the leg would never tick;
    * ``instrument_type`` = the leg's ``side`` (``CE`` / ``PE`` / ``FUT``);
    * ``underlying_symbol`` = the contract's ``underlying`` (so the shard knows
      its parent).

    Parameters
    ----------
    contract
        The registry :class:`~brutex.data.registry_store.ContractRow`.
    feed_token
        The numeric ``exchange_token`` string resolved for this leg.
    """
    return {
        "exchange": str(contract.exch),
        "segment": _LEG_SEGMENT,
        "exchange_token": feed_token,
        "instrument_type": str(contract.side),
        "underlying_symbol": contract.underlying,
    }


def build_leg_rows(
    registry_path: Path, *, resolve_feed_token: FeedTokenResolver = _no_feed_token
) -> list[dict[str, Any]]:
    """Return the F&O leg feed-rows from the ``registry.duckdb`` cache.

    Reads the SEPARATE registry cache (the legs the lean master discards) read-only
    via :meth:`~brutex.data.registry_store.RegistryStore.list_contracts`. A MISSING
    / not-yet-pulled registry file degrades to ``[]`` — never opens / creates a file
    just to read it, never invents legs (Golden Rule #1), mirroring the ``/db``
    registry routes' empty path.

    Each leg's numeric feed ``exchange_token`` is resolved from its
    ``contract_symbol`` via *resolve_feed_token*; a leg the resolver cannot map
    (``None`` — unknown / expired) is SKIPPED rather than emitted with a guessed
    token (Golden Rule #1). With the default :func:`_no_feed_token` no leg
    resolves, so ``legs`` degrades to ``[]`` until a production resolver is wired.

    Parameters
    ----------
    registry_path
        The sibling ``registry.duckdb`` path (see
        :func:`brutex.dashboard.db_view.registry_path_for`).
    resolve_feed_token
        Maps a contract ``contract_symbol`` to its numeric ``exchange_token``
        string, or ``None`` when the symbol is unknown / expired.

    Returns
    -------
    list[dict[str, Any]]
        One leg feed-row per RESOLVABLE persisted contract (``CE`` / ``PE`` /
        ``FUT``), or ``[]`` when no registry exists yet / nothing resolves.
    """
    if not registry_path.is_file():
        return []
    store = RegistryStore.open(registry_path)
    try:
        rows: list[dict[str, Any]] = []
        for contract in store.list_contracts():
            token = resolve_feed_token(contract.contract_symbol)
            if token is not None:
                rows.append(_leg_row(contract, token))
        return rows
    finally:
        store.close()


def build_manifest(
    spot: Sequence[Mapping[str, Any]],
    legs: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Assemble the ``{"spot": [...], "legs": [...]}`` manifest from row sequences.

    Pure — takes already-built feed-row sequences and returns the manifest dict
    the capture's ``read_subscription_manifest`` parses. Each row is copied into a
    plain ``dict`` so the manifest never aliases a caller's mutable row.
    """
    return {
        "spot": [dict(row) for row in spot],
        "legs": [dict(row) for row in legs],
    }


def build_live_manifest(
    store: Any,
    registry_path: Path,
    *,
    resolve_feed_token: FeedTokenResolver = _no_feed_token,
) -> dict[str, list[dict[str, Any]]]:
    """Build the full live manifest from the instrument store + the F&O registry.

    The dashboard-side composition: the subscribable spot universe
    (:func:`build_spot_rows`) + the F&O legs (:func:`build_leg_rows`, ``[]`` when
    no registry is pulled yet / nothing resolves). Read-only.

    Parameters
    ----------
    store
        The wired instrument store (the spot source).
    registry_path
        The sibling ``registry.duckdb`` path (the legs source).
    resolve_feed_token
        Maps a leg ``contract_symbol`` to its numeric feed ``exchange_token``
        (``None`` → skip the leg). Defaults to :func:`_no_feed_token` (legs
        degrade to ``[]`` until a production resolver is wired).

    Returns
    -------
    dict[str, list[dict[str, Any]]]
        The ``{"spot", "legs"}`` manifest.
    """
    spot = build_spot_rows(store)
    legs = build_leg_rows(registry_path, resolve_feed_token=resolve_feed_token)
    _LOG.info("live_manifest_built", spot=len(spot), legs=len(legs))
    return build_manifest(spot, legs)


def make_store_manifest_writer(
    instrument_store: Any, *, resolve_feed_token: FeedTokenResolver = _no_feed_token
) -> Callable[[str], None]:
    """Return a ``manifest_writer(path)`` bound to *instrument_store* + its registry.

    The dashboard injects this into :class:`~brutex.dashboard.live_runner.LiveRunner`
    so the ON toggle writes the EXACT persisted 775 + the F&O legs to the capture's
    ``--manifest`` path. The registry is resolved as the master's sibling
    (:func:`brutex.dashboard.db_view.registry_path_for`). With NO store wired
    (``instrument_store is None``) the writer emits an EMPTY manifest
    (``{spot: [], legs: []}``) — the capture then resolves the spot from the
    instrument master itself (its documented no-manifest fallback), never an
    invented row (Golden Rule #1). This builder is the COMMON wiring point so the
    closure stays testable offline.

    Parameters
    ----------
    instrument_store
        The wired store, or ``None`` when the dashboard runs without a lake.
    resolve_feed_token
        Maps a leg ``contract_symbol`` to its numeric feed ``exchange_token``
        (``None`` → skip the leg). Defaults to :func:`_no_feed_token`; the
        dashboard wires :func:`make_master_feed_token_resolver` so real legs
        resolve, degrading honestly to a skipped leg on a miss.

    Returns
    -------
    Callable[[str], None]
        A ``manifest_writer`` that writes the JSON manifest to the given path.
    """

    def _writer(path: str) -> None:
        if instrument_store is None:
            write_manifest(path, build_manifest([], []))
            return
        store_path = getattr(instrument_store, "path", None)
        registry_path = (
            registry_path_for(store_path)
            if isinstance(store_path, Path)
            else Path(_FALLBACK_REGISTRY_NAME)
        )
        write_manifest(
            path,
            build_live_manifest(
                instrument_store, registry_path, resolve_feed_token=resolve_feed_token
            ),
        )

    return _writer


def _index_feed_token(index: Any, symbol: str) -> str | None:
    """Map *symbol* to its numeric feed token via an ``InstrumentIndex``-like *index*.

    Pure mapping logic (no I/O): looks the contract ``groww_symbol`` up in
    *index*'s ``by_groww_symbol``; on a hit returns the matched instrument's
    ``security_id`` as a string (the numeric instrument-master token an F&O leg
    subscribes on — ``docs/groww/api-reference/07-feed.md:54``), on a miss
    (unknown / expired) returns ``None`` so the leg is skipped (Golden Rule #1).
    """
    instrument = index.by_groww_symbol.get(symbol)
    if instrument is None:
        return None
    return str(instrument.security_id)


def _load_master_index() -> Any:  # pragma: no cover — network/CSV load
    """Download + index the FULL Groww instrument master (the live ``by_groww_symbol``).

    The only un-covered surface — it needs the live master CSV — mirroring the
    ``# pragma`` boundary on the live CLI's ``_real_instrument_resolver``.
    """
    from brutex.adapters.groww.instrument_loader import load_index, urllib_http_get

    return load_index(urllib_http_get)


def make_master_feed_token_resolver(
    *, load_index: Callable[[], Any] = _load_master_index
) -> FeedTokenResolver:
    """Return a :data:`FeedTokenResolver` backed by the FULL Groww instrument master.

    On the FIRST leg resolution it lazily loads the instrument master (via
    *load_index*, which defaults to the live SSRF-guarded
    :func:`_load_master_index`) and closes over its O(1) ``by_groww_symbol``
    index, so building the resolver is free (no network at dashboard boot) and the
    master is fetched only when a manifest with legs is actually written — and only
    once (the loaded index is cached for every subsequent leg). Each leg
    ``contract_symbol`` then maps → its numeric ``exchange_token`` string via the
    pure :func:`_index_feed_token`; a leg absent from the master (unknown /
    expired) resolves to ``None`` and is skipped (Golden Rule #1).

    Parameters
    ----------
    load_index
        Zero-arg loader returning an ``InstrumentIndex``-like object exposing
        ``by_groww_symbol``. Defaults to the live network load; tests inject a
        fake so the cache + mapping wiring is covered without a network call.
    """
    cache: dict[str, Any] = {}

    def _resolve(symbol: str) -> str | None:
        if "index" not in cache:
            cache["index"] = load_index()
        return _index_feed_token(cache["index"], symbol)

    return _resolve


def write_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    """Write *manifest* to *path* as JSON (atomic temp + replace), return the path.

    The capture subprocess reads this exact file via ``--manifest <path>``.
    Written atomically (temp sibling + ``replace``) so the capture never reads a
    torn half-written manifest. Parent directories are created as needed.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest), encoding="utf-8")
    tmp.replace(out)
    return out


__all__ = [
    "FeedTokenResolver",
    "build_leg_rows",
    "build_live_manifest",
    "build_manifest",
    "build_spot_rows",
    "make_master_feed_token_resolver",
    "make_store_manifest_writer",
    "write_manifest",
]
