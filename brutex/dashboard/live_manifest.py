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


def _spot_row(instrument: Any) -> dict[str, Any]:
    """Project one in-scope :class:`~brutex.models.Instrument` to a feed-row dict.

    Maps the persisted store columns to the four manifest feed keys the capture
    validates — ``exchange`` (``exch_id``), ``segment``, ``exchange_token``
    (``security_id``), ``instrument_type`` (``instrument``) — plus the optional
    ``underlying_symbol`` so a ``"both"`` spot connection can route each row to
    the feed that delivers it (indices → index-value, stocks → ltp).
    """
    return {
        "exchange": str(instrument.exch_id),
        "segment": str(instrument.segment),
        "exchange_token": str(instrument.security_id),
        "instrument_type": str(instrument.instrument),
        "underlying_symbol": instrument.underlying_symbol,
    }


def build_spot_rows(store: Any) -> list[dict[str, Any]]:
    """Return the in-scope spot feed-rows (the 775) from the instrument store.

    Reads ``store.list_in_scope()`` — the latest-snapshot, in-scope universe —
    and keeps only the SUBSCRIBABLE spot types (``IDX`` / ``EQ``); FUT / CE / PE
    legs (if any survived in the master) are excluded (they are the manifest's
    ``legs``, sourced from the registry instead). Read-only.

    Parameters
    ----------
    store
        The wired :class:`~brutex.data.instrument_store.InstrumentStore` (its
        ``list_in_scope`` is the latest-snapshot in-scope reader).

    Returns
    -------
    list[dict[str, Any]]
        One feed-row per subscribable spot instrument, in the store's
        ``list_in_scope`` order (stable for an idempotent manifest).
    """
    rows: list[dict[str, Any]] = []
    for instrument in store.list_in_scope():
        if str(instrument.instrument) in _SPOT_INSTRUMENT_TYPES:
            rows.append(_spot_row(instrument))
    return rows


def _leg_row(contract: Any) -> dict[str, Any]:
    """Project one registry :class:`~brutex.data.registry_store.ContractRow` to a leg feed-row.

    The registry persists the addressing identity ``(exch, contract_symbol,
    side)`` but no numeric token / segment, so the feed-row stamps:

    * ``exchange`` = the contract's ``exch``;
    * ``segment`` = :data:`_LEG_SEGMENT` (every leg is F&O);
    * ``exchange_token`` = the ``contract_symbol`` (the leg's stable addressing
      string — the same value the capture shards + dedups on);
    * ``instrument_type`` = the leg's ``side`` (``CE`` / ``PE`` / ``FUT``);
    * ``underlying_symbol`` = the contract's ``underlying`` (so the shard knows
      its parent).
    """
    return {
        "exchange": str(contract.exch),
        "segment": _LEG_SEGMENT,
        "exchange_token": str(contract.contract_symbol),
        "instrument_type": str(contract.side),
        "underlying_symbol": contract.underlying,
    }


def build_leg_rows(registry_path: Path) -> list[dict[str, Any]]:
    """Return the F&O leg feed-rows from the ``registry.duckdb`` cache.

    Reads the SEPARATE registry cache (the legs the lean master discards) read-only
    via :meth:`~brutex.data.registry_store.RegistryStore.list_contracts`. A MISSING
    / not-yet-pulled registry file degrades to ``[]`` — never opens / creates a file
    just to read it, never invents legs (Golden Rule #1), mirroring the ``/db``
    registry routes' empty path.

    Parameters
    ----------
    registry_path
        The sibling ``registry.duckdb`` path (see
        :func:`brutex.dashboard.db_view.registry_path_for`).

    Returns
    -------
    list[dict[str, Any]]
        One leg feed-row per persisted contract (``CE`` / ``PE`` / ``FUT``), or
        ``[]`` when no registry exists yet.
    """
    if not registry_path.is_file():
        return []
    store = RegistryStore.open(registry_path)
    try:
        return [_leg_row(contract) for contract in store.list_contracts()]
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


def build_live_manifest(store: Any, registry_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Build the full live manifest from the instrument store + the F&O registry.

    The dashboard-side composition: the subscribable spot universe
    (:func:`build_spot_rows`) + the F&O legs (:func:`build_leg_rows`, ``[]`` when
    no registry is pulled yet). Read-only.

    Parameters
    ----------
    store
        The wired instrument store (the spot source).
    registry_path
        The sibling ``registry.duckdb`` path (the legs source).

    Returns
    -------
    dict[str, list[dict[str, Any]]]
        The ``{"spot", "legs"}`` manifest.
    """
    spot = build_spot_rows(store)
    legs = build_leg_rows(registry_path)
    _LOG.info("live_manifest_built", spot=len(spot), legs=len(legs))
    return build_manifest(spot, legs)


def make_store_manifest_writer(instrument_store: Any) -> Callable[[str], None]:
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
        write_manifest(path, build_live_manifest(instrument_store, registry_path))

    return _writer


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
    "build_leg_rows",
    "build_live_manifest",
    "build_manifest",
    "build_spot_rows",
    "make_store_manifest_writer",
    "write_manifest",
]
