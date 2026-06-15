"""Open N websocket connections per a ``ConnectionPlan`` and subscribe each shard.

The operator's scaling model: **connection #0 carries the whole spot universe**
(the admitted indices + their cash equities — indices on the index-value feed +
stocks on the ltp feed, ``--feed both``, ``DEC-LIVE-DUAL-FEED-001``); **connections
#1..N carry the F&O leg shards** on the ltp feed. This module wires
:func:`~brutex.live.plan.build_grouped_plan` (the spot-first sharding) into a real
multi-connection subscribe + a PER-CONNECTION subscription report, so the operator
can SEE "how many connections opened, what each subscribed, and (at market open)
which are ticking" — the honest answer to "are all my N connections + their
subscriptions really working?".

Decoupling + testability (CLAUDE.md §11 COMMON, ``DEC-LIVE-DECOUPLE-001``): the
sharding (:func:`plan_connections`) and the open+subscribe orchestration
(:func:`open_and_subscribe`) take INJECTED ``open_feed`` / ``make_subscribe``
collaborators, so the whole flow is unit-tested with fakes — no ``growwapi``, no
network. Only the real feed wiring at the CLI boundary
(:mod:`brutex.live.__main__`) is ``# pragma: no cover``.

Honest O(1) envelope (Golden Rule #10): every per-instrument route/assign on the
returned plan is O(1) (dict); BUILDING the bindings is O(N) one pass over the N
distinct instruments — NOT an O(1)-total claim. Read-only market data; no orders.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from brutex.live.capture import SubscribeFn
from brutex.live.plan import (
    DEFAULT_PER_CONNECTION_CAP,
    ConnectionPlan,
    Instrument,
    build_grouped_plan,
    connection_count,
)
from brutex.live.probe import ConnectionProbeResult

_LOG = structlog.get_logger("brutex.live.multiconn")

#: Feed mode for the spot connection(s) — indices + stocks, each on its own feed.
SPOT_FEED_KIND = "both"
#: Feed mode for the leg connections — futures/options carry a last-traded-price.
LEG_FEED_KIND = "ltp"

#: Open a websocket connection for the given 0-based ordinal, returning the feed.
OpenFeedFn = Callable[[int], Any]
#: Build a 0-arg-callback subscriber for ``(feed, instruments, feed_kind)``.
MakeSubscribeFn = Callable[[Any, Sequence[Mapping[str, Any]], str], SubscribeFn]


#: The feed keys every subscription-manifest row must carry.
_MANIFEST_ROW_KEYS: tuple[str, ...] = ("exchange", "segment", "exchange_token", "instrument_type")


def _validate_manifest_rows(rows: object, *, group: str) -> list[dict[str, Any]]:
    """Coerce + validate one manifest group into feed-row dicts (raises on a bad row)."""
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError(f"manifest {group!r} must be a list, got {type(rows).__name__}")
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError(f"manifest {group!r} row must be an object, got {type(row).__name__}")
        missing = [key for key in _MANIFEST_ROW_KEYS if key not in row]
        if missing:
            raise ValueError(f"manifest {group!r} row missing keys {missing}: {dict(row)}")
        out.append(dict(row))
    return out


def read_subscription_manifest(
    path: str | Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a ``{"spot": [...], "legs": [...]}`` JSON manifest → ``(spot, legs)``.

    The dashboard — which owns the instrument store (the exact 775 spot
    underlyings) + the F&O ``contracts`` registry (the option/future legs) —
    writes this manifest; the capture process reads it. Keeping the manifest a
    PLAIN JSON keeps ``brutex/live/`` DECOUPLED from the lake schema
    (``DEC-LIVE-DECOUPLE-001``): it never imports the dashboard or the data plane,
    just parses a file of feed-row dicts. Each row must carry the four feed keys
    (``exchange``, ``segment``, ``exchange_token``, ``instrument_type``) so it can
    be classified (index-value vs ltp) + sharded.

    Raises
    ------
    ValueError
        If the manifest is not a JSON object, a group is not a list, or any row
        is not an object / is missing a required feed key.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        raise ValueError(f"manifest must be a JSON object, got {type(data).__name__}")
    spot = _validate_manifest_rows(data.get("spot", []), group="spot")
    legs = _validate_manifest_rows(data.get("legs", []), group="legs")
    return spot, legs


def probe_meta_block(probe_result: ConnectionProbeResult) -> dict[str, Any]:
    """Project a :class:`~brutex.live.probe.ConnectionProbeResult` to the ``meta.probe`` block.

    Emits the EXACT shape the dashboard reader
    (:func:`brutex.dashboard.live_plan_view.read_connection_plan` →
    ``_probe_from_meta``) consumes — ``{"max_held": int, "failed_at": int|None,
    "reached_ceiling": bool}`` — so a stamped manifest surfaces the REAL measured
    concurrent-connection ceiling on ``/live`` with NO dashboard change
    (DEC-LIVE-PROBE-MANIFEST-001). The ``failure`` reason string is deliberately
    NOT carried: the reader keys only on the numeric ceiling, and the human-readable
    reason already lands in the ``connection_probe`` structured log line.

    This is a PURE projection of an ALREADY-RUN probe result; it never fabricates a
    ceiling (Golden Rule #1) — the caller only invokes the stamp after a real probe.
    """
    return {
        "max_held": probe_result.max_held,
        "failed_at": probe_result.failed_at,
        "reached_ceiling": probe_result.reached_ceiling,
    }


def merge_probe_into_manifest(
    manifest: Mapping[str, Any], probe_result: ConnectionProbeResult
) -> dict[str, Any]:
    """Return a NEW manifest dict with ``meta.probe`` stamped from *probe_result*.

    A pure deep-merge: every existing top-level block (``spot`` / ``legs`` /
    anything else) and every existing ``meta`` key (e.g. the writer's ``meta.plan``,
    DEC-LIVE-DASH-PROBE-001) is PRESERVED — only ``meta.probe`` is set/replaced with
    the projection of *probe_result*. A malformed existing ``meta`` (not a mapping)
    is replaced by a fresh ``meta`` carrying only ``probe`` rather than crashing.

    PURE: every preserved block is :func:`copy.deepcopy`-ed, so the returned dict
    shares NO mutable sub-object with *manifest* — a later mutation of the result
    can never corrupt the caller's input (and vice-versa). Re-stamping the SAME
    *probe_result* yields an equal dict (the projection is a deterministic function
    of the result), which keeps :func:`stamp_probe_manifest_file` byte-idempotent.
    """
    merged: dict[str, Any] = {
        key: copy.deepcopy(value) for key, value in manifest.items() if key != "meta"
    }
    existing_meta = manifest.get("meta")
    new_meta: dict[str, Any] = (
        copy.deepcopy(dict(existing_meta)) if isinstance(existing_meta, Mapping) else {}
    )
    new_meta["probe"] = probe_meta_block(probe_result)
    merged["meta"] = new_meta
    return merged


def stamp_probe_manifest_file(
    manifest_path: str | Path, probe_result: ConnectionProbeResult
) -> bool:
    """Stamp a REAL *probe_result*'s ceiling into the manifest at *manifest_path*.

    The probe→manifest wiring for DEC-LIVE-PROBE-MANIFEST-001: after
    ``python -m brutex.live probe-connections --manifest <path>`` runs a REAL
    connection ramp, this writes the measured concurrent-connection ceiling into the
    manifest's ``meta.probe`` block — the EXACT shape the merged dashboard panel
    (``GET /api/v1/live/plan`` → :func:`brutex.dashboard.live_plan_view.read_connection_plan`)
    reads, so ``/live`` surfaces the ACCOUNT ceiling instead of "probe not run", with
    NO dashboard-reader change.

    Decoupling (``DEC-LIVE-DECOUPLE-001``): this lives on the WRITER / live side and
    imports nothing from the dashboard — the manifest JSON is the file contract. The
    existing ``spot`` / ``legs`` / ``meta.plan`` blocks are PRESERVED (deep-merge via
    :func:`merge_probe_into_manifest`); only ``meta.probe`` is set.

    Honesty (Golden Rule #1): a MISSING / unreadable / non-object manifest is LEFT
    UNTOUCHED and ``False`` returned — the dashboard keeps showing "probe not run"
    rather than a fabricated ceiling. The stamp is performed ONLY here, after a real
    probe, so no default/guessed ceiling is ever written.

    Idempotence (Golden Rule #7): the manifest is rewritten with canonical,
    sort-stable JSON, so stamping the SAME *probe_result* a second time produces
    byte-identical file content.

    Parameters
    ----------
    manifest_path
        The capture's ``--manifest`` JSON path (the dashboard's ON-toggle sink).
    probe_result
        The outcome of a REAL :func:`~brutex.live.probe.probe_connections` ramp.

    Returns
    -------
    bool
        ``True`` when the manifest was stamped; ``False`` when it was absent /
        unreadable / not a JSON object (left untouched — honest absence).
    """
    path = Path(manifest_path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        _LOG.warning("probe_manifest_absent", path=str(path), max_held=probe_result.max_held)
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        _LOG.warning("probe_manifest_unreadable", path=str(path), max_held=probe_result.max_held)
        return False
    if not isinstance(data, Mapping):
        _LOG.warning("probe_manifest_not_object", path=str(path), max_held=probe_result.max_held)
        return False
    merged = merge_probe_into_manifest(data, probe_result)
    # Canonical, deterministic bytes so re-stamping the same probe is byte-identical
    # (Golden Rule #7). sort_keys keeps the ordering stable regardless of how the
    # dashboard writer ordered the original keys; no trailing newline (mirrors
    # live_manifest.write_manifest's compact json.dumps). Written atomically
    # (temp sibling + os.replace) — mirroring live_manifest.write_manifest — so the
    # dashboard's concurrent read can never see a torn half-written manifest.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(merged, sort_keys=True), encoding="utf-8")
    tmp.replace(path)
    _LOG.info(
        "probe_manifest_stamped",
        path=str(path),
        max_held=probe_result.max_held,
        failed_at=probe_result.failed_at,
        reached_ceiling=probe_result.reached_ceiling,
    )
    return True


def _instrument_of(row: Mapping[str, Any]) -> Instrument:
    """Build a plan :class:`Instrument` from a resolver row (its feed identity)."""
    underlying = row.get("underlying_symbol") or row.get("underlying")
    return Instrument(
        exchange=str(row["exchange"]),
        segment=str(row["segment"]),
        exchange_token=str(row["exchange_token"]),
        underlying=str(underlying) if underlying else None,
    )


@dataclass(frozen=True, slots=True)
class ConnectionBinding:
    """One websocket connection's assignment: which instruments, on which feed.

    Attributes
    ----------
    connection_index
        0-based connection ordinal (0 = the first spot connection).
    feed_kind
        ``"both"`` for the spot connection(s) (index-value + ltp), ``"ltp"`` for
        the leg connections.
    instruments
        The resolver rows (WITH ``instrument_type``, so a ``"both"`` connection
        can route each to its feed) assigned to this connection.
    """

    connection_index: int
    feed_kind: str
    instruments: tuple[Mapping[str, Any], ...]

    @property
    def size(self) -> int:
        """How many instruments this connection subscribes."""
        return len(self.instruments)

    @property
    def is_spot(self) -> bool:
        """True for the spot connection(s) (dual-feed)."""
        return self.feed_kind == SPOT_FEED_KIND


def plan_connections(
    spot: Sequence[Mapping[str, Any]],
    legs: Sequence[Mapping[str, Any]],
    *,
    per_connection_cap: int = DEFAULT_PER_CONNECTION_CAP,
    ceiling: int | None = None,
) -> tuple[list[ConnectionBinding], int, ConnectionPlan]:
    """Shard *spot* + *legs* into per-connection bindings (spot first).

    Builds the spot-first grouped plan (:func:`build_grouped_plan`) so the spot
    group fills connection #0.. and the legs the connections AFTER — never mixed
    in one shard. Connection #0.. (the spot shards) get ``feed_kind="both"``; the
    leg shards get ``"ltp"``. Each binding carries the ORIGINAL resolver rows (so a
    spot connection can dual-feed-route them), mapped back from the plan's deduped
    instruments by their stable key — **spot wins** on a cross-group duplicate
    (mirroring :func:`build_grouped_plan`).

    Parameters
    ----------
    ceiling
        If given, stop binding after this many connections (the probe-discovered
        concurrent connection ceiling); the count of SKIPPED shards is returned so
        the report is honest about what did NOT get a connection.

    Returns
    -------
    (bindings, skipped, plan)
        The per-connection bindings, how many shards exceeded *ceiling*, and the
        underlying :class:`ConnectionPlan` (for the O(1) ``connection_of`` lookups).

    Raises
    ------
    ValueError
        If *ceiling* is negative (a cap of 0 means "no connections", which is
        valid — it yields zero bindings and skips every shard).
    """
    if ceiling is not None and ceiling < 0:
        raise ValueError(f"ceiling must be >= 0, got {ceiling}")
    # Map each feed key → its resolver row; legs first so a spot row WINS on a
    # cross-group duplicate (the same instrument is kept on its spot connection).
    row_by_key: dict[str, Mapping[str, Any]] = {}
    for row in (*legs, *spot):
        row_by_key[_instrument_of(row).key] = row
    spot_instruments = [_instrument_of(r) for r in spot]
    leg_instruments = [_instrument_of(r) for r in legs]
    plan = build_grouped_plan(
        spot_instruments, leg_instruments, per_connection_cap=per_connection_cap
    )
    distinct_spot = len({inst.key for inst in spot_instruments})
    num_spot_shards = connection_count(distinct_spot, per_connection_cap=per_connection_cap)
    bindings: list[ConnectionBinding] = []
    skipped = 0
    for index in range(plan.num_connections):
        if ceiling is not None and index >= ceiling:
            skipped = plan.num_connections - index
            break
        feed_kind = SPOT_FEED_KIND if index < num_spot_shards else LEG_FEED_KIND
        rows = tuple(row_by_key[inst.key] for inst in plan.shard(index))
        bindings.append(ConnectionBinding(index, feed_kind, rows))
    return bindings, skipped, plan


@dataclass(frozen=True, slots=True)
class OpenedConnection:
    """A binding whose websocket connection is OPEN + subscribed."""

    binding: ConnectionBinding
    feed: Any


def open_and_subscribe(
    bindings: Sequence[ConnectionBinding],
    *,
    open_feed: OpenFeedFn,
    make_subscribe: MakeSubscribeFn,
    callback: Callable[[], None],
) -> list[OpenedConnection]:
    """Open a websocket per *binding* and subscribe its instruments on its feed.

    For each binding: open a fresh connection (``open_feed(index)``), build the
    feed-aware subscriber (``make_subscribe(feed, instruments, feed_kind)`` — the
    spot binding's ``"both"`` routes indices to the index-value feed + stocks to
    ltp; a leg binding's ``"ltp"`` subscribes all on ltp), and register *callback*.
    Every opened connection feeds the SAME callback (one capture sink). Returns the
    opened connections so the caller can read snapshots + report per connection.
    """
    opened: list[OpenedConnection] = []
    for binding in bindings:
        feed = open_feed(binding.connection_index)
        make_subscribe(feed, list(binding.instruments), binding.feed_kind)(callback)
        _LOG.info(
            "connection_subscribed",
            connection=binding.connection_index,
            feed_kind=binding.feed_kind,
            instruments=binding.size,
        )
        opened.append(OpenedConnection(binding, feed))
    return opened


#: Pull one connection's snapshot for its feed kind (``feed, feed_kind`` → mapping).
SnapshotOfFn = Callable[[Any, str], Mapping[str, Any]]


def make_multiconn_capture(
    bindings: Sequence[ConnectionBinding],
    *,
    open_feed: OpenFeedFn,
    make_subscribe: MakeSubscribeFn,
    snapshot_of: SnapshotOfFn,
) -> tuple[
    Callable[[Callable[[], None]], None], Callable[[], dict[str, Any]], list[OpenedConnection]
]:
    """Wire N connections into the SINGLE ``(subscribe, read_snapshot)`` capture seam.

    Returns ``(subscribe, read_snapshot, opened)`` for :class:`~brutex.live.capture.TickCapture`:

    * ``subscribe(callback)`` opens + subscribes EVERY binding's connection, all
      feeding *callback* (and records them in the returned ``opened`` list);
    * ``read_snapshot()`` merges each opened connection's snapshot
      (``snapshot_of(feed, feed_kind)``) into one mapping — so a single capture
      record carries every instrument from every connection.

    The capture loop is unchanged (COMMON reuse) — this just composes its two
    closures over many connections instead of one.
    """
    opened: list[OpenedConnection] = []

    def subscribe(callback: Callable[[], None]) -> None:
        opened.extend(
            open_and_subscribe(
                bindings, open_feed=open_feed, make_subscribe=make_subscribe, callback=callback
            )
        )

    def read_snapshot() -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for connection in opened:
            merged.update(snapshot_of(connection.feed, connection.binding.feed_kind))
        return merged

    return subscribe, read_snapshot, opened


@dataclass(frozen=True, slots=True)
class ConnectionRow:
    """Per-connection line of the subscription proof: index, feed, count."""

    connection_index: int
    feed_kind: str
    subscribed: int


@dataclass(frozen=True, slots=True)
class SubscriptionPlanReport:
    """The N-connection subscription proof (the operator's 'are they all working?').

    Attributes
    ----------
    connections
        Per-connection ``(index, feed_kind, subscribed)`` rows, in order.
    skipped_shards
        Shards that exceeded the connection ceiling (got NO connection) — surfaced,
        never hidden (Golden Rule #1).
    total_instruments
        Distinct instruments across every bound connection.
    """

    connections: tuple[ConnectionRow, ...]
    skipped_shards: int
    total_instruments: int

    @property
    def num_connections(self) -> int:
        """How many connections were bound (opened + subscribed)."""
        return len(self.connections)

    def summary(self) -> str:
        """One-line human verdict for logs / the CLI / the dashboard."""
        spot = sum(1 for row in self.connections if row.feed_kind == SPOT_FEED_KIND)
        legs = self.num_connections - spot
        tail = f", {self.skipped_shards} shard(s) over the ceiling" if self.skipped_shards else ""
        return (
            f"{self.num_connections} connections "
            f"({spot} spot + {legs} leg), {self.total_instruments} instruments{tail}"
        )

    def render_markdown(self) -> str:
        """Render the per-connection subscription proof as a Markdown table."""
        lines = [
            "# Live-feed connection subscription plan",
            "",
            f"**{self.summary()}**",
            "",
            "| Connection | Feed | Instruments |",
            "|---|---|---|",
        ]
        for row in self.connections:
            lines.append(f"| #{row.connection_index} | {row.feed_kind} | {row.subscribed} |")
        return "\n".join(lines) + "\n"


def build_report(bindings: Sequence[ConnectionBinding], skipped: int) -> SubscriptionPlanReport:
    """Summarise *bindings* into the per-connection subscription proof."""
    rows = tuple(ConnectionRow(b.connection_index, b.feed_kind, b.size) for b in bindings)
    return SubscriptionPlanReport(
        connections=rows,
        skipped_shards=skipped,
        total_instruments=sum(row.subscribed for row in rows),
    )


__all__ = [
    "LEG_FEED_KIND",
    "SPOT_FEED_KIND",
    "ConnectionBinding",
    "ConnectionRow",
    "OpenedConnection",
    "SubscriptionPlanReport",
    "build_report",
    "make_multiconn_capture",
    "merge_probe_into_manifest",
    "open_and_subscribe",
    "plan_connections",
    "probe_meta_block",
    "read_subscription_manifest",
    "stamp_probe_manifest_file",
]
