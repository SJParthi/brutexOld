"""``python -m brutex.live`` — the operator-runnable live-feed parity CLI.

Five subcommands cover the full "capture ticks → build candles → compare to
backtest" loop plus the scalability/cadence diagnostics:

* ``capture``  — subscribe to the live feed and append every snapshot to a JSONL
  log (run this from 09:15 IST; read-only, no orders).
* ``cadence``  — analyse a captured log: are ticks arriving in ms or seconds?
* ``build``    — fold a captured log into 1-minute candles (09:15–15:29 IST).
* ``parity``   — diff live candles vs exported backtest candles → a table report.
* ``probe-connections`` — ramp websocket connections 1..N to find the ceiling.

The Groww-specific, network-bound steps (building the real feed, resolving the
instrument tokens from the master) are isolated behind :class:`LiveDeps` defaults
marked ``# pragma: no cover`` — exactly mirroring
:mod:`brutex.adapters.groww.live`. Every command handler takes injected deps, so
the dispatch is unit-tested end-to-end with a fake feed (no ``growwapi``, no
network). No ``print`` — status goes through ``structlog`` (the banned-pattern
gate), and the human tables are written to files.
"""

from __future__ import annotations

import argparse
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from brutex.live.candle_builder import build_minute_candles
from brutex.live.capture import SnapshotFn, SubscribeFn, TickCapture, _utc_now
from brutex.live.lake import minute_tick_counts, write_live_candles, write_live_ticks
from brutex.live.multiconn import (
    build_report,
    make_multiconn_capture,
    plan_connections,
    read_subscription_manifest,
)
from brutex.live.parity import compare_candles
from brutex.live.probe import analyze_cadence, probe_connections
from brutex.live.report import render_parity_markdown
from brutex.live.rolling import (
    RollingTickStore,
    fold_segment,
    list_segments,
    make_fold_on_rotate,
    segment_dir_for,
)
from brutex.live.serde import read_candles, write_candles
from brutex.live.store import TickStore
from brutex.live.tick import iter_ticks
from brutex.live.verify import render_subscription_markdown, verify_subscription

_LOG = structlog.get_logger("brutex.live.cli")

#: The 4 brute-force instruments — the index-value PARITY target (vs the backtest
#: candles, which only carry the 3 indexes + India VIX). Use this with
#: ``--feed index`` for parity; the capture default is the FULL universe.
PARITY_SYMBOLS = "NSE-NIFTY,NSE-BANKNIFTY,BSE-SENSEX,NSE-INDIAVIX"
#: Default capture/verify symbol selector — the FULL universe (every index +
#: every F&O-eligible stock), resolved feed-aware by ``_real_instrument_resolver``.
DEFAULT_SYMBOLS = "all"
#: Where the live CLI tees its structured logs (sibling of the dashboard's log).
DEFAULT_LOG_FILE = ".brutex-logs/brutex-live.log"


def _noop_configure_logging(_log_file: Path | None) -> None:
    """Default logging hook — a no-op (the real file/console sink is injected)."""


@dataclass
class LiveDeps:
    """Injected side-effecting collaborators (fakes in tests, real on the Mac)."""

    feed_builder: Callable[[], Any]
    instrument_resolver: Callable[[str, str], Sequence[Mapping[str, Any]]]
    clock: Callable[[], datetime] = _utc_now
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    configure_logging: Callable[[Path | None], None] = _noop_configure_logging


def _real_feed_builder() -> Any:  # pragma: no cover — needs growwapi + creds + network
    """Build a live ``GrowwFeed`` from env credentials (TOTP flow)."""
    import os

    from growwapi import GrowwAPI, GrowwFeed  # type: ignore[import-not-found, unused-ignore]

    from brutex.adapters.groww.auth import get_access_token, resolve_groww_credentials

    creds = resolve_groww_credentials(os.environ)
    token = get_access_token(creds).access_token
    return GrowwFeed(GrowwAPI(token))


def _real_instrument_resolver(  # pragma: no cover — needs the instrument master
    symbols: str, feed_kind: str
) -> list[dict[str, Any]]:
    """Resolve ``EXCH-SYMBOL`` strings (or a sentinel) to feed instrument dicts.

    Downloads the Groww instrument master and emits the ``{exchange, segment,
    exchange_token}`` dicts the feed's ``subscribe_*`` methods expect. The
    selector is **feed-aware** so each feed only gets instruments it can carry:

    * ``"all"`` / ``"universe"`` — the FULL brute-force-adjacent universe. With
      ``--feed index`` this is every index (``IDX``) value; with ``--feed ltp``
      it ALSO adds every F&O-eligible single stock (an ``EQ`` row whose symbol is
      named by a ``FUT``/``CE``/``PE`` leg) — indices + stocks in one run.
    * ``"everything"`` / ``"all-equity"`` — indices PLUS *every* NSE/BSE equity
      (the ~750 the dashboard shows), for the widest ltp capture.
    * a comma list of ``EXCH-SYMBOL`` — exactly those ``groww_symbol`` rows.

    Stocks are never subscribed on the index-value feed (they would be silent);
    ``verify`` reports any genuinely-silent instrument rather than hiding it.
    """
    import csv
    import urllib.request

    from brutex.adapters.groww.constants import GROWW_INSTRUMENT_CSV_URL

    text = urllib.request.urlopen(GROWW_INSTRUMENT_CSV_URL, timeout=30).read().decode("utf-8")  # noqa: S310  # nosec B310
    rows = list(csv.DictReader(text.splitlines()))
    sentinel = symbols.strip().lower()
    all_equity = sentinel in ("everything", "all-equity")
    full = all_equity or sentinel in ("all", "universe")
    on_ltp = feed_kind != "index"
    fno_underlyings = {
        (r.get("underlying_symbol") or "").strip().upper()
        for r in rows
        if (r.get("instrument_type") or "").strip().upper() in ("FUT", "CE", "PE")
    }
    wanted = {s.strip().upper() for s in symbols.split(",") if s.strip()}
    out: list[dict[str, Any]] = []
    for row in rows:
        itype = (row.get("instrument_type") or "").strip().upper()
        gsym = (row.get("groww_symbol") or "").strip().upper()
        tsym = (row.get("trading_symbol") or "").strip().upper()
        if full:
            stock_ok = on_ltp and (all_equity or tsym in fno_underlyings)
            keep = itype == "IDX" or (itype == "EQ" and stock_ok)
        else:
            keep = gsym in wanted
        if keep:
            out.append(
                {
                    "exchange": row["exchange"],
                    "segment": row["segment"],
                    "exchange_token": row["exchange_token"],
                    # Tag the feed routing: indices ride the index-VALUE feed, every
                    # other instrument the LTP feed (consumed by ``--feed both``).
                    "instrument_type": itype,
                }
            )
    return out


def _real_configure_logging(log_file: Path | None) -> None:  # pragma: no cover — global I/O
    """Tee the live CLI's structured logs to the console AND a JSONL file.

    Mirrors the dashboard's structured-log contract (one JSON object per line) so
    an unattended ``capture`` / ``verify`` / ``probe-connections`` run leaves a
    durable, greppable trail — not just ephemeral console output.
    """
    import logging
    import sys

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(format="%(message)s", level=logging.INFO, handlers=handlers, force=True)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _real_deps() -> LiveDeps:  # pragma: no cover — wires the real network collaborators
    """The production dependency bundle (real feed + real instrument master)."""
    return LiveDeps(
        feed_builder=_real_feed_builder,
        instrument_resolver=_real_instrument_resolver,
        configure_logging=_real_configure_logging,
    )


#: Groww instrument-master ``instrument_type`` whose rows carry an index VALUE
#: (not a last-traded-price) and so MUST ride the index-value feed.
_INDEX_FEED_TYPE = "IDX"
#: The three keys the SDK ``subscribe_*`` methods accept (drop our routing tag).
_FEED_KEYS = ("exchange", "segment", "exchange_token")


def classify_feed_instruments(
    instruments: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split resolver instruments into ``(index_value_rows, ltp_rows)`` by type.

    Indices (``instrument_type == "IDX"``) carry an index VALUE, not a
    last-traded-price, so they would be SILENT on the LTP feed — they must ride
    ``subscribe_index_value``. Every other instrument (equities, F&O legs) rides
    ``subscribe_ltp``. Each returned dict is stripped to :data:`_FEED_KEYS` (our
    ``instrument_type`` routing tag is dropped before it reaches the SDK).

    This is the fix for the operator-observed gap where a single ``--feed ltp``
    run subscribed the indices on the wrong feed, so they never ticked.
    """
    index_rows: list[dict[str, Any]] = []
    ltp_rows: list[dict[str, Any]] = []
    for inst in instruments:
        feed_row = {key: inst[key] for key in _FEED_KEYS}
        itype = str(inst.get("instrument_type", "")).strip().upper()
        target = index_rows if itype == _INDEX_FEED_TYPE else ltp_rows
        target.append(feed_row)
    return index_rows, ltp_rows


def merge_snapshots(*snapshots: Mapping[str, Any]) -> dict[str, Any]:
    """Union several feed snapshots into one mapping (index-value + ltp combined).

    The two feeds key on DISJOINT instruments, so this is a straight union; the
    last writer wins on the (not-expected) key collision, keeping it total.
    """
    merged: dict[str, Any] = {}
    for snapshot in snapshots:
        merged.update(snapshot)
    return merged


def _make_subscribe(
    feed: Any, instruments: Sequence[Mapping[str, Any]], feed_kind: str
) -> SubscribeFn:
    """Return a 0-arg-callback subscriber bound to *feed* for *feed_kind*.

    ``"both"`` routes indices to ``subscribe_index_value`` and everything else to
    ``subscribe_ltp`` on the SAME feed/callback (one connection, two
    subscriptions) — so a single capture carries the whole spot universe with
    each instrument on the feed that actually delivers it.
    """
    if feed_kind == "both":
        index_rows, ltp_rows = classify_feed_instruments(instruments)

        def subscribe_both(callback: Callable[[], None]) -> None:
            if index_rows:
                feed.subscribe_index_value(index_rows, on_data_received=callback)
            if ltp_rows:
                feed.subscribe_ltp(ltp_rows, on_data_received=callback)

        return subscribe_both

    method = feed.subscribe_ltp if feed_kind == "ltp" else feed.subscribe_index_value

    def subscribe(callback: Callable[[], None]) -> None:
        method(list(instruments), on_data_received=callback)

    return subscribe


def _make_snapshot_reader(feed: Any, feed_kind: str) -> SnapshotFn:
    """Return the snapshot reader for *feed_kind*.

    ``"both"`` merges the index-value AND ltp snapshots so the capture callback
    persists every instrument from either feed in one record.
    """
    if feed_kind == "both":

        def read_both() -> Mapping[str, Any]:
            return merge_snapshots(feed.get_index_value(), feed.get_ltp())

        return read_both
    if feed_kind == "ltp":
        return feed.get_ltp  # type: ignore[no-any-return]
    return feed.get_index_value  # type: ignore[no-any-return]


def _capture_with_rolling(
    args: argparse.Namespace,
    deps: LiveDeps,
    subscribe: SubscribeFn,
    read_snapshot: SnapshotFn,
) -> int:
    """Capture into a ROTATING segment set, auto-folding each rotated segment.

    The bounded/rolling sink (``DEC-LIVE-AUTOROLL-001``) keeps no single JSONL
    file unbounded: it rotates at ``--roll-max-bytes`` / ``--roll-max-lines`` and,
    when ``--roll-lake-root`` is given, folds-then-prunes each rotated segment to
    the live Parquet lake (else a bounded raw ring of ``--roll-max-segments``).
    The end-of-session ACTIVE tail is flushed + folded too.
    """
    on_rotate = (
        make_fold_on_rotate(
            lake_root=args.roll_lake_root,
            capture_run_id=args.run_id,
            feed_kind=_fold_feed(args.feed),
        )
        if args.roll_lake_root
        else None
    )
    store = RollingTickStore.open(
        args.out,
        max_bytes=args.roll_max_bytes,
        max_lines=args.roll_max_lines,
        max_segments=args.roll_max_segments,
        on_rotate=on_rotate,
        resume=args.resume,
    )
    capture = TickCapture(
        store=store, subscribe=subscribe, read_snapshot=read_snapshot, clock=deps.clock
    )
    captured = capture.run_for(
        args.seconds, sleep=deps.sleep, monotonic=deps.monotonic, heartbeat_interval=args.heartbeat
    )
    tail = store.flush_active()
    store.close()
    _LOG.info(
        "capture_rolling_complete",
        captured=captured,
        segment_dir=str(segment_dir_for(args.out)),
        rotations=store.rotations,
        tail_folded=tail is not None,
        lake_root=str(args.roll_lake_root) if args.roll_lake_root else None,
    )
    return 0


def _cmd_capture(args: argparse.Namespace, deps: LiveDeps) -> int:
    """Subscribe + capture every snapshot for ``--seconds`` to ``--out``."""
    deps.configure_logging(args.log_file)
    feed = deps.feed_builder()
    instruments = deps.instrument_resolver(args.symbols, args.feed)
    _LOG.info(
        "capture_subscribe", instruments=len(instruments), feed=args.feed, symbols=args.symbols
    )
    subscribe = _make_subscribe(feed, instruments, args.feed)
    read_snapshot = _make_snapshot_reader(feed, args.feed)
    if args.roll:
        return _capture_with_rolling(args, deps, subscribe, read_snapshot)
    with TickStore.open(args.out, resume=args.resume) as store:
        capture = TickCapture(
            store=store,
            subscribe=subscribe,
            read_snapshot=read_snapshot,
            clock=deps.clock,
        )
        captured = capture.run_for(
            args.seconds,
            sleep=deps.sleep,
            monotonic=deps.monotonic,
            heartbeat_interval=args.heartbeat,
        )
    stats = analyze_cadence(TickStore.read(args.out))
    _LOG.info(
        "capture_complete",
        captured=captured,
        out=str(args.out),
        cadence=stats.classification,
        median_interval_ms=stats.median_interval_ms,
    )
    return 0


def _fold_feed(feed_kind: str) -> str:
    """Map a capture ``--feed`` (index/ltp/both) to the lake ``feed_kind`` tag.

    The lake's ``feed_kind`` is a free-form provenance stamp; ``both`` carries
    mixed instruments, so it is recorded as ``"both"`` (the writer does not
    branch on it — it is purely descriptive).
    """
    return feed_kind


def _cmd_cadence(args: argparse.Namespace, _deps: LiveDeps) -> int:
    """Report tick cadence (ms vs seconds) for a captured log."""
    stats = analyze_cadence(TickStore.read(args.in_path))
    _LOG.info(
        "cadence",
        samples=stats.samples,
        classification=stats.classification,
        median_interval_ms=stats.median_interval_ms,
        min_interval_ms=stats.min_interval_ms,
        max_interval_ms=stats.max_interval_ms,
        duration_seconds=stats.duration_seconds,
    )
    return 0


def _cmd_build(args: argparse.Namespace, _deps: LiveDeps) -> int:
    """Fold a captured log into 1-minute candles → ``--out`` JSON."""
    session_date = (
        datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=UTC).date() if args.date else None
    )
    records = TickStore.read(args.in_path)
    candles = build_minute_candles(iter_ticks(records), session_date=session_date)
    write_candles(args.out, candles)
    _LOG.info(
        "build_complete",
        instruments=len(candles),
        bars={key: len(bars) for key, bars in candles.items()},
        out=str(args.out),
    )
    return 0


def _cmd_parity(args: argparse.Namespace, _deps: LiveDeps) -> int:
    """Diff live candles vs backtest candles → a Markdown table at ``--out``."""
    live = read_candles(args.live)
    backtest = read_candles(args.backtest)
    reports = [
        compare_candles(
            live.get(key, []),
            backtest.get(key, []),
            instrument_key=key,
        )
        for key in sorted(set(live) | set(backtest))
    ]
    markdown = render_parity_markdown(reports)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(markdown, encoding="utf-8")
    identical = all(report.is_identical for report in reports)
    _LOG.info(
        "parity_complete",
        instruments=len(reports),
        all_identical=identical,
        out=str(args.out),
    )
    return 0 if identical else 1


def _cmd_probe_connections(args: argparse.Namespace, deps: LiveDeps) -> int:
    """Ramp websocket connections 1..``--max`` to find the ceiling."""
    deps.configure_logging(args.log_file)

    def open_connection(ordinal: int) -> Any:
        conn = deps.feed_builder()
        _LOG.info("connection_opened", ordinal=ordinal)
        return conn

    result = probe_connections(open_connection=open_connection, max_conns=args.max)
    _LOG.info(
        "connection_probe",
        max_held=result.max_held,
        failed_at=result.failed_at,
        failure=result.failure,
        reached_ceiling=result.reached_ceiling,
    )
    return 0


def _cmd_lake(args: argparse.Namespace, _deps: LiveDeps) -> int:
    """Persist a captured JSONL log into the ``live_ticks`` + ``live_candles_1m`` lake."""
    records = TickStore.read(args.in_path)
    tick_paths = write_live_ticks(
        records, lake_root=args.lake_root, capture_run_id=args.run_id, feed_kind=args.feed
    )
    candles = build_minute_candles(iter_ticks(records))
    candle_paths = write_live_candles(
        candles,
        lake_root=args.lake_root,
        built_run_id=args.run_id,
        tick_counts=minute_tick_counts(records),
    )
    _LOG.info(
        "lake_complete",
        records=len(records),
        tick_partitions=len(tick_paths),
        candle_partitions=len(candle_paths),
        instruments=len(candles),
        lake_root=str(args.lake_root),
    )
    return 0


def _cmd_roll_fold(args: argparse.Namespace, _deps: LiveDeps) -> int:
    """Fold-then-prune EVERY segment under a rolling-capture directory into the lake.

    The crash-recovery / catch-up path for ``DEC-LIVE-AUTOROLL-001``: after a
    crash (or a capture run with no ``--roll-lake-root``) the segment directory
    may hold un-folded ``seg-*.jsonl`` files. This folds each into the live lake
    (REUSING the ``write_live_*`` writers via :func:`fold_segment`) and prunes it
    after a verified write. Re-running is idempotent (the lake PK dedups a re-fold)
    — already-pruned segments are simply absent. ``--keep`` retains the raw
    segments (fold without prune).
    """
    directory = segment_dir_for(args.in_path)
    segments = list_segments(directory)
    folded = 0
    pruned = 0
    records = 0
    for segment in segments:
        result = fold_segment(
            segment,
            lake_root=args.lake_root,
            capture_run_id=args.run_id,
            feed_kind=args.feed,
            prune=not args.keep,
        )
        folded += 1
        records += result.records
        pruned += int(result.pruned)
    _LOG.info(
        "roll_fold_complete",
        segment_dir=str(directory),
        segments=folded,
        records=records,
        pruned=pruned,
        lake_root=str(args.lake_root),
    )
    return 0


def _cmd_verify(args: argparse.Namespace, deps: LiveDeps) -> int:
    """Subscribe to ALL requested instruments + PROVE per-instrument tick delivery."""
    deps.configure_logging(args.log_file)
    feed = deps.feed_builder()
    instruments = deps.instrument_resolver(args.symbols, args.feed)
    _LOG.info(
        "verify_subscribe", instruments=len(instruments), feed=args.feed, symbols=args.symbols
    )
    subscribe = _make_subscribe(feed, instruments, args.feed)
    read_snapshot = _make_snapshot_reader(feed, args.feed)
    with TickStore.open(args.out, resume=args.resume) as store:
        report = verify_subscription(
            store=store,
            subscribe=subscribe,
            read_snapshot=read_snapshot,
            requested=len(instruments),
            seconds=args.seconds,
            sleep=deps.sleep,
            monotonic=deps.monotonic,
            clock=deps.clock,
        )
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(render_subscription_markdown(report), encoding="utf-8")
    _LOG.info(
        "verify_complete",
        requested=report.requested,
        observed=report.observed,
        total_ticks=report.total_ticks,
        silent=report.silent,
        all_ticking=report.all_requested_ticking,
        report=str(args.report),
    )
    return 0 if report.all_requested_ticking else 1


def _cmd_capture_all(  # pragma: no cover — opens N real websockets (operator's Mac)
    args: argparse.Namespace, deps: LiveDeps
) -> int:
    """Open N connections per the grouped plan + capture, writing the per-connection proof.

    Connection #0 carries the spot universe (``--feed both``: indices on the
    index-value feed + stocks on ltp); connections #1..N carry the F&O leg shards
    on ltp (``DEC-LIVE-MULTICONN-001``). Each connection feeds ONE
    :class:`~brutex.live.capture.TickCapture` (the loop is unchanged). The
    per-connection subscription proof is logged + written to ``--report`` so the
    operator can SEE how many connections opened + what each subscribed (and, at
    market open, which are ticking). Leg resolution (``get_expiries`` →
    ``get_contracts``) is the named follow-up; until then *legs* is empty and the
    plan is the spot connection(s).
    """
    deps.configure_logging(args.log_file)
    spot: Sequence[Mapping[str, Any]]
    legs: Sequence[Mapping[str, Any]]
    if args.manifest:
        # The dashboard wrote the EXACT 775 spot + the F&O legs from the persisted
        # instruments + contracts tables (DEC-LIVE-MULTICONN-MANIFEST-001).
        spot, legs = read_subscription_manifest(args.manifest)
    else:
        # No manifest → resolve the spot from the instrument master; legs empty
        # (connection #0 = spot only).
        spot, legs = deps.instrument_resolver(args.symbols, "both"), []
    bindings, skipped, plan = plan_connections(
        spot, legs, per_connection_cap=args.cap, ceiling=args.ceiling
    )
    report = build_report(bindings, skipped)
    _LOG.info(
        "capture_all_plan",
        connections=report.num_connections,
        instruments=report.total_instruments,
        skipped_shards=report.skipped_shards,
        ceiling=args.ceiling,
    )
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report.render_markdown(), encoding="utf-8")

    subscribe, read_snapshot, _opened = make_multiconn_capture(
        bindings,
        open_feed=lambda _ordinal: deps.feed_builder(),
        make_subscribe=_make_subscribe,
        snapshot_of=lambda feed, feed_kind: _make_snapshot_reader(feed, feed_kind)(),
    )
    with TickStore.open(args.out, resume=args.resume) as store:
        capture = TickCapture(store=store, subscribe=subscribe, read_snapshot=read_snapshot)
        captured = capture.run_for(
            args.seconds,
            sleep=deps.sleep,
            monotonic=deps.monotonic,
            heartbeat_interval=args.heartbeat,
        )
    _LOG.info(
        "capture_all_complete",
        captured=captured,
        connections=report.num_connections,
        plan_connections=plan.num_connections,
        out=str(args.out),
        report=str(args.report),
    )
    return 0


_HANDLERS: dict[str, Callable[[argparse.Namespace, LiveDeps], int]] = {
    "capture": _cmd_capture,
    "capture-all": _cmd_capture_all,
    "cadence": _cmd_cadence,
    "build": _cmd_build,
    "parity": _cmd_parity,
    "probe-connections": _cmd_probe_connections,
    "lake": _cmd_lake,
    "roll-fold": _cmd_roll_fold,
    "verify": _cmd_verify,
}


def _add_roll_args(parser: argparse.ArgumentParser) -> None:
    """Add the bounded/rolling capture flags (DEC-LIVE-AUTOROLL-001) to *parser*."""
    parser.add_argument(
        "--roll",
        action="store_true",
        help="bounded/rolling sink: rotate --out into seg-NNNNNN.jsonl segments "
        "(no single GB file) + auto-fold each rotated segment to the lake",
    )
    parser.add_argument(
        "--roll-max-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="rotate the active segment at this byte size (default 64 MiB)",
    )
    parser.add_argument(
        "--roll-max-lines",
        type=int,
        default=1_000_000,
        help="rotate the active segment at this line count (default 1,000,000)",
    )
    parser.add_argument(
        "--roll-max-segments",
        type=int,
        default=8,
        help="when NO --roll-lake-root, keep at most this many raw segments (ring)",
    )
    parser.add_argument(
        "--roll-lake-root",
        dest="roll_lake_root",
        default=None,
        help="fold-then-prune each rotated segment into this live lake root",
    )
    parser.add_argument(
        "--run-id", dest="run_id", default="live", help="capture/fold run id (lake provenance)"
    )


def _build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser for all subcommands."""
    parser = argparse.ArgumentParser(prog="brutex.live", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="capture live ticks to a JSONL log")
    cap.add_argument("--out", required=True, help="destination JSONL path")
    cap.add_argument("--seconds", type=float, default=60.0, help="capture duration")
    cap.add_argument(
        "--symbols",
        default=DEFAULT_SYMBOLS,
        help=f'"all"/"universe" (every index + F&O stock), "everything" (every '
        f"equity too), or a comma EXCH-SYMBOL list (parity subset: {PARITY_SYMBOLS})",
    )
    cap.add_argument(
        "--feed",
        choices=("index", "ltp", "both"),
        default="both",
        help="both = indices on the index-value feed + stocks on the ltp feed "
        "(the whole spot universe, each on the feed that delivers it — the default); "
        "ltp = last-traded-price only (indices would be silent); "
        "index = index-value only (parity-accurate for the 3 indexes)",
    )
    cap.add_argument(
        "--heartbeat", type=float, default=30.0, help="progress-log interval (seconds)"
    )
    cap.add_argument("--log-file", dest="log_file", type=Path, default=Path(DEFAULT_LOG_FILE))
    cap.add_argument("--resume", action="store_true", help="append to an existing log")
    _add_roll_args(cap)

    cal = sub.add_parser(
        "capture-all", help="open N connections (spot on #0, legs on #1..N) + capture"
    )
    cal.add_argument("--out", required=True, help="destination JSONL path")
    cal.add_argument("--report", required=True, help="per-connection subscription proof (Markdown)")
    cal.add_argument("--seconds", type=float, default=60.0, help="capture duration")
    cal.add_argument("--symbols", default=DEFAULT_SYMBOLS, help="spot selector (see capture)")
    cal.add_argument(
        "--manifest",
        default=None,
        help='JSON {"spot":[...],"legs":[...]} of the exact 775 + F&O legs '
        "(the dashboard writes it from the instruments + contracts tables); "
        "overrides --symbols when given",
    )
    cal.add_argument(
        "--cap", type=int, default=1000, help="per-connection subscription cap (default 1000)"
    )
    cal.add_argument(
        "--ceiling",
        type=int,
        default=None,
        help="max concurrent connections (the probe ceiling; default: no limit)",
    )
    cal.add_argument("--heartbeat", type=float, default=30.0, help="progress-log interval (s)")
    cal.add_argument("--log-file", dest="log_file", type=Path, default=Path(DEFAULT_LOG_FILE))
    cal.add_argument("--resume", action="store_true", help="append to an existing log")

    cad = sub.add_parser("cadence", help="report tick cadence (ms vs seconds)")
    cad.add_argument("--in", dest="in_path", required=True, help="captured JSONL path")

    bld = sub.add_parser("build", help="fold ticks into 1-minute candles")
    bld.add_argument("--in", dest="in_path", required=True, help="captured JSONL path")
    bld.add_argument("--out", required=True, help="destination candle JSON path")
    bld.add_argument("--date", default=None, help="IST session date YYYY-MM-DD filter")

    par = sub.add_parser("parity", help="diff live vs backtest candles")
    par.add_argument("--live", required=True, help="live candle JSON (from build)")
    par.add_argument("--backtest", required=True, help="backtest candle JSON")
    par.add_argument("--out", required=True, help="destination Markdown report path")

    prb = sub.add_parser("probe-connections", help="ramp connections 1..N")
    prb.add_argument("--max", type=int, default=100, help="max connections to try")
    prb.add_argument("--log-file", dest="log_file", type=Path, default=Path(DEFAULT_LOG_FILE))

    lk = sub.add_parser("lake", help="persist a captured log into the live lake tables")
    lk.add_argument("--in", dest="in_path", required=True, help="captured JSONL path")
    lk.add_argument("--lake-root", dest="lake_root", required=True, help="lake root directory")
    lk.add_argument("--run-id", dest="run_id", required=True, help="capture/build run id")
    lk.add_argument("--feed", choices=("index", "ltp"), default="index")

    rf = sub.add_parser(
        "roll-fold", help="fold-then-prune every segment in a rolling-capture directory"
    )
    rf.add_argument(
        "--in", dest="in_path", required=True, help="rolling-capture base path or segment dir"
    )
    rf.add_argument("--lake-root", dest="lake_root", required=True, help="lake root directory")
    rf.add_argument("--run-id", dest="run_id", required=True, help="capture/fold run id")
    rf.add_argument("--feed", choices=("index", "ltp", "both"), default="index")
    rf.add_argument("--keep", action="store_true", help="fold WITHOUT pruning the raw segments")

    ver = sub.add_parser("verify", help="PROVE the feed subscribes + ticks (per instrument)")
    ver.add_argument(
        "--out", required=True, help="JSONL capture path (the self-test also captures)"
    )
    ver.add_argument("--report", required=True, help="destination Markdown proof path")
    ver.add_argument("--symbols", default="all", help='"all" (full universe) or comma EXCH-SYMBOL')
    ver.add_argument("--seconds", type=float, default=60.0, help="measurement window")
    ver.add_argument("--feed", choices=("index", "ltp", "both"), default="both")
    ver.add_argument("--log-file", dest="log_file", type=Path, default=Path(DEFAULT_LOG_FILE))
    ver.add_argument("--resume", action="store_true", help="append to an existing log")

    return parser


def main(argv: Sequence[str] | None = None, *, deps: LiveDeps | None = None) -> int:
    """Parse *argv* and dispatch to the matching command handler.

    Parameters
    ----------
    argv
        Argument vector (defaults to ``sys.argv[1:]`` via argparse).
    deps
        Injected collaborators; defaults to the real network bundle.

    Returns
    -------
    int
        Process exit code (0 = success; ``parity`` returns 1 on divergence).
    """
    resolved = deps if deps is not None else _real_deps()
    args = _build_parser().parse_args(argv)
    return _HANDLERS[args.command](args, resolved)


if __name__ == "__main__":  # pragma: no cover — module entry point
    raise SystemExit(main())
