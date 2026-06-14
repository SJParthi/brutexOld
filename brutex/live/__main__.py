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
from brutex.live.parity import compare_candles
from brutex.live.probe import analyze_cadence, probe_connections
from brutex.live.report import render_parity_markdown
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

    from growwapi import GrowwAPI, GrowwFeed  # type: ignore[import-not-found]

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


def _make_subscribe(
    feed: Any, instruments: Sequence[Mapping[str, Any]], feed_kind: str
) -> SubscribeFn:
    """Return a 0-arg-callback subscriber bound to *feed* for *feed_kind*."""
    method = feed.subscribe_ltp if feed_kind == "ltp" else feed.subscribe_index_value

    def subscribe(callback: Callable[[], None]) -> None:
        method(list(instruments), on_data_received=callback)

    return subscribe


def _make_snapshot_reader(feed: Any, feed_kind: str) -> SnapshotFn:
    """Return the snapshot reader for *feed_kind* (``get_ltp`` / ``get_index_value``)."""
    if feed_kind == "ltp":
        return feed.get_ltp  # type: ignore[no-any-return]
    return feed.get_index_value  # type: ignore[no-any-return]


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


_HANDLERS: dict[str, Callable[[argparse.Namespace, LiveDeps], int]] = {
    "capture": _cmd_capture,
    "cadence": _cmd_cadence,
    "build": _cmd_build,
    "parity": _cmd_parity,
    "probe-connections": _cmd_probe_connections,
    "lake": _cmd_lake,
    "verify": _cmd_verify,
}


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
        choices=("index", "ltp"),
        default="ltp",
        help="ltp = index + stock last-traded-price (whole universe); "
        "index = index-value feed (parity-accurate for the 3 indexes)",
    )
    cap.add_argument(
        "--heartbeat", type=float, default=30.0, help="progress-log interval (seconds)"
    )
    cap.add_argument("--log-file", dest="log_file", type=Path, default=Path(DEFAULT_LOG_FILE))
    cap.add_argument("--resume", action="store_true", help="append to an existing log")

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

    ver = sub.add_parser("verify", help="PROVE the feed subscribes + ticks (per instrument)")
    ver.add_argument(
        "--out", required=True, help="JSONL capture path (the self-test also captures)"
    )
    ver.add_argument("--report", required=True, help="destination Markdown proof path")
    ver.add_argument("--symbols", default="all", help='"all" (full universe) or comma EXCH-SYMBOL')
    ver.add_argument("--seconds", type=float, default=60.0, help="measurement window")
    ver.add_argument("--feed", choices=("index", "ltp"), default="ltp")
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
