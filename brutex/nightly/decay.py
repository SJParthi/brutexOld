"""The nightly R48 strategy-decay leg (LANE W5, DEC-NIGHTLY-DECAY-R48-001).

Closes the R48 deferral (lifted by the winning-P0 operator ruling,
2026-07-18): after the chain, the nightly re-measures the NEWEST sweep's
``top100.json`` finalists over a TRAILING window of recent sessions and
issues a per-entry decay verdict under the lockbox tolerance law
(:mod:`brutex.nightly.decay_math` — the born-Rust verdict kernels).

The leg is a REPORT-LEVEL block (the DEC-BOX-AUTODEPLOY-LANE-001 #1939
precedent), NEVER a chain link: it runs post-chain inside
``run.py:_run_locked``'s ``finally`` (before ``_assemble_report``),
fail-open — ANY failure becomes an honest ``{"status": "error", ...}``
block + one honesty-note line, never a broken nightly.

The mechanics
-------------
1. Discover the newest run dir holding a valid ``top100.json`` across the
   :func:`brutex.nightly.sweep_binding.contract_run_roots` law (the
   documented v1.1 seam) and :func:`~brutex.engine.top100_artifact.load_top100`.
2. Per entry: resolve the lake coordinates from the engine spot-triples law
   (:data:`brutex.universe.ENGINE_SWEEP_SPOT_TRIPLES` — an out-of-scope
   instrument is SKIPPED + counted, never guessed), load the trailing bars
   once per ``(instrument, tf)`` (:func:`~brutex.engine.lake_slice.load_lake_slice`,
   bounded read; trimmed to the last :data:`DECAY_TRAILING_SESSIONS`
   distinct IST session dates PRESENT in the lake — data-driven, no
   calendar walk), rebuild the Stage-1A matrix
   (:func:`~brutex.engine.stage1a.build_bar_bits`) and re-simulate the
   entry's mask with the EXISTING trade-sim kernel
   (:func:`~brutex.engine.trade_sim.simulate_trades` +
   :func:`~brutex.engine.combo_mask.mask_to_entry_signals` — the same seam
   ``trade_capture.capture_combo`` wraps; the per-combo parity tooth is
   N/A on a trailing SUB-window, a disclosed deviation from full-window
   capture).
3. Fold the per-trade returns (``pnl_paisa / start_equity_paisa`` — the
   ``metrics/compute.py`` basis) via the kernel and judge
   ``trailing vs discovery`` with :func:`~brutex.nightly.decay_math.decay_verdict_token`.
4. Persist ``decay-diff.json`` ATOMICALLY into the run's artifact dir (an
   ADDITIVE sidecar beside ``top100.json`` — the artifact + its stamp are
   NEVER rewritten; R13: rows are kept forever, annotated never hidden),
   emit ONE aggregated ``strategy_decayed`` Telegram on any breach
   (best-effort), and return the report block.

Honest envelope (Golden Rule #10)
---------------------------------
* Cost: ``O(|top100| · trailing_bars)`` oracle re-sim + one bounded lake
  read per distinct ``(instrument, tf)`` — trivial beside the sweep;
  NEVER claimed O(1)-total.
* A trailing window holds FEW trades: verdicts are tolerance tests with
  honest low-N ABSTAIN codes (never DSR / cascade filters, which cannot
  say anything at low N; an abstain is NEVER a DECAYED verdict).
* The top100 entry does not carry the sweep's non-default Stage-1A axes
  (``prior_n`` / ``fib_anchor`` / VWAP+EMA price sources), so the re-sim
  evaluates the DEFAULT cell (the production engine is cardinality-1
  default, CLAUDE.md #1681) — stamped per block as
  ``axis_defaults_assumed`` (true), a disclosed limit.
* The verdict annotates; it never re-ranks, rewrites or hides the stored
  artifact (R13). Retirement is the champions leg's annotation.
* NEGATIVE-discovery-Sharpe quirk (round-1 #6, accepted-disclosed): with
  a negative discovery Sharpe the ``0.70×`` floor sits ABOVE discovery
  (``0.70 × -1.0 = -0.7 > -1.0``), so a trailing Sharpe equal to a
  negative discovery reads DECAYED — faithful to the ONE lockbox
  tolerance formula, and such inputs should never reach the leg (a
  negative-Sharpe finalist cannot pass the ranking gates into top100).
"""

from __future__ import annotations

import contextlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.nightly.decay_math import (
    VERDICT_DECAYED,
    decay_verdict_token,
    resolve_decay_min_trades,
    trailing_fold,
)
from brutex.nightly.sweep_binding import contract_run_roots

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brutex.data.catalog import Catalog
    from brutex.data.models import CandleBar
    from brutex.notifications.sweep_alerts import SweepAlerter

logger = structlog.get_logger(__name__)

__all__ = [
    "DECAY_DIFF_FILENAME",
    "DECAY_ENABLED_DEFAULT",
    "DECAY_ENABLED_ENV",
    "DECAY_TRAILING_SESSIONS",
    "DECAY_TRAILING_SESSIONS_ENV",
    "DiscoveredRun",
    "discover_top100_runs",
    "entry_join_key",
    "instrument_coords",
    "nightly_decay_enabled",
    "resolve_trailing_sessions",
    "run_decay_leg",
    "run_decay_leg_guarded",
    "trailing_session_bars",
]

#: The leg's kill-switch env flag (tristate; junk is FAIL-LOUD — a raise
#: the guarded wrapper folds into the honest ``env_error`` block).
DECAY_ENABLED_ENV: Final[str] = "BRUTEX_NIGHTLY_DECAY"

#: Default ON (flipped in-branch once the end-to-end goldens were green —
#: the greenfield ruling; the falsy env literal is the kill switch).
DECAY_ENABLED_DEFAULT: Final[bool] = True

#: The trailing-window session count (~3 trading months — the task brief's
#: ~63 sessions; a W5-default standing in for an un-ruled operator value).
DECAY_TRAILING_SESSIONS: Final[int] = 63

#: Fail-loud env override of :data:`DECAY_TRAILING_SESSIONS`.
DECAY_TRAILING_SESSIONS_ENV: Final[str] = "BRUTEX_DECAY_TRAILING_SESSIONS"

#: The durable per-run decay sidecar (beside ``top100.json``, additive).
DECAY_DIFF_FILENAME: Final[str] = "decay-diff.json"

#: Calendar-day over-read factor: NSE trades ~5 of 7 days, so reading
#: ``ceil(sessions * 7 / 5) + 15`` calendar days back guarantees at least
#: ``sessions`` session dates when the lake has them (holiday slack).
_CALENDAR_SLACK_DAYS: Final[int] = 15

#: A trailing window needs at least two sessions to say anything.
_MIN_TRAILING_SESSIONS: Final[int] = 2


def nightly_decay_enabled() -> bool:
    """Resolve ``BRUTEX_NIGHTLY_DECAY`` (fail-loud tristate).

    Raises
    ------
    ValueError
        On an unrecognised literal — the guarded wrapper folds this into
        the report's honest ``env_error`` block (loud, never chain-breaking).
    """
    import os

    raw = os.environ.get(DECAY_ENABLED_ENV)
    if raw is None or not raw.strip():
        return DECAY_ENABLED_DEFAULT
    verdict = env_flag_tristate(raw)
    if verdict is None:
        raise ValueError(
            f"{DECAY_ENABLED_ENV}={raw!r} is not a recognised flag literal "
            "(use 1/true/yes/on or 0/false/no/off)"
        )
    return verdict


def resolve_trailing_sessions() -> int:
    """Resolve the trailing session count (env override, fail-loud on junk)."""
    import os

    raw = os.environ.get(DECAY_TRAILING_SESSIONS_ENV)
    if raw is None or not raw.strip():
        return DECAY_TRAILING_SESSIONS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{DECAY_TRAILING_SESSIONS_ENV}={raw!r} is not an integer — set a whole number >= 2"
        ) from exc
    if value < _MIN_TRAILING_SESSIONS:
        raise ValueError(f"{DECAY_TRAILING_SESSIONS_ENV}={raw!r} must be >= 2")
    return value


@dataclass(frozen=True, slots=True)
class DiscoveredRun:
    """One discovered ``sweep_runs/<run_id>/`` artifact dir (newest-first)."""

    runs_dir: Path
    run_id: str
    mtime: float


def discover_top100_runs(results_root: Path, *, limit: int) -> tuple[DiscoveredRun, ...]:
    """The newest run dirs holding a ``top100.json`` file, newest-first.

    Presence-only discovery over the :func:`contract_run_roots` law
    (validation is :func:`load_top100`'s job); deterministic order
    ``(top100 mtime, path)`` descending; unreadable dirs are skipped
    silently (the probe's own ``suppress(OSError)`` posture).

    The sort keys on the **top100.json FILE mtime** — the artifact is
    IMMUTABLE once written (R13) — deliberately NOT the run-DIR mtime,
    which the legs' own sidecar writes (``run-delta.json`` /
    ``decay-diff.json``) mutate: a dir-mtime key let a prior night's
    sidecar-touched dir outrank a later run (round-1 MINOR #2, fixed).
    The path tie-break keeps the order deterministic on equal mtimes.
    """
    candidates: list[tuple[float, str, Path]] = []
    for root in contract_run_roots(results_root):
        with contextlib.suppress(OSError):
            for child in root.iterdir():
                if not child.is_dir() or not (child / "top100.json").is_file():
                    continue
                with contextlib.suppress(OSError):
                    candidates.append(((child / "top100.json").stat().st_mtime, str(child), child))
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return tuple(
        DiscoveredRun(runs_dir=child.parent, run_id=child.name, mtime=mtime)
        for mtime, _, child in candidates[: max(0, limit)]
    )


def instrument_coords(instrument: str) -> tuple[str, str] | None:
    """``(exchange, segment)`` for an engine spot instrument, else ``None``.

    Resolved from :data:`brutex.universe.ENGINE_SWEEP_SPOT_TRIPLES` (the
    scope law — never a hand-typed mapping). ``None`` = not an engine spot
    symbol (a research-escape artifact) — the caller SKIPS it honestly.
    """
    from brutex.universe import ENGINE_SWEEP_SPOT_TRIPLES

    for exchange, segment, symbol in sorted(ENGINE_SWEEP_SPOT_TRIPLES):
        if symbol == instrument:
            return exchange, segment
    return None


def entry_join_key(entry: Mapping[str, object]) -> str:
    """The run-delta / champions join identity of one top100 entry.

    The Rule-4 run-identity fields MINUS the snapshot + commit hashes
    (``data_snapshot_hash`` / ``code_commit_sha`` re-key every night, so a
    join on the full ``run_id`` would NEVER match across nights):
    ``mask_hex | direction | instrument | tf | sl | tp`` (the arm carries
    the canonical trade params persisted per entry; ``mode`` is constant
    within an artifact).

    HONEST LIMIT (round-1 MINOR #3): the key OMITS the non-default
    Stage-1A axis params (``fib_anchor`` / ``prior_n`` / the
    ``PriceSource`` cells) because the top100 entry schema provably does
    NOT carry them (verified against ``top100_artifact._entry_payload``
    — the entry keys are exactly the binding contract set), so two
    finalists differing ONLY in an axis COLLIDE on this key. Until the
    schema carries the axis fragments (named follow-up
    DEC-TOP100-AXIS-JOIN-KEY-001) every dict keyed on this function
    rides the LOUD collision guard
    (:func:`brutex.nightly.champions._entries_by_join_key` — count +
    WARN + keep-first, never a silent overwrite). Today's production
    engine is cardinality-1 DEFAULT-cell (CLAUDE.md #1681), so a real
    artifact cannot mix axes — the guard is defense-in-depth.
    """
    arm = entry.get("arm")
    arm_map: Mapping[str, object] = arm if isinstance(arm, Mapping) else {}
    return "|".join(
        (
            str(entry.get("mask_hex")),
            str(entry.get("direction")),
            str(entry.get("instrument")),
            str(entry.get("tf")),
            str(arm_map.get("sl_paisa")),
            str(arm_map.get("tp_paisa")),
        )
    )


def trailing_session_bars(bars: Sequence[CandleBar], sessions: int) -> list[CandleBar]:
    """The bars of the LAST *sessions* distinct IST session dates present.

    Data-driven session trim (no calendar dependency): a session = one
    distinct IST calendar date carrying at least one bar. Fewer distinct
    dates than *sessions* returns every bar (the caller's low-N abstain
    owns the honesty).
    """
    from brutex.calendars.nse import IST

    dates = [bar.timestamp.astimezone(IST).date() for bar in bars]
    unique = list(dict.fromkeys(dates))
    if len(unique) <= sessions:
        return list(bars)
    keep = set(unique[-sessions:])
    return [bar for bar, day in zip(bars, dates, strict=True) if day in keep]


def _entry_arm(entry: Mapping[str, object]) -> tuple[int | None, int | None]:
    """The entry's ``(sl_paisa, tp_paisa)`` (``None`` legs pass through)."""
    arm = entry.get("arm")
    arm_map: Mapping[str, object] = arm if isinstance(arm, Mapping) else {}
    sl = arm_map.get("sl_paisa")
    tp = arm_map.get("tp_paisa")
    return (sl if isinstance(sl, int) else None, tp if isinstance(tp, int) else None)


def _discovery_metrics(entry: Mapping[str, object]) -> tuple[float | None, int | None]:
    """The entry's discovery ``(sharpe_ratio, max_drawdown_paisa)``."""
    metrics = entry.get("metrics")
    metrics_map: Mapping[str, object] = metrics if isinstance(metrics, Mapping) else {}
    sharpe = metrics_map.get("sharpe_ratio")
    max_dd = metrics_map.get("max_drawdown_paisa")
    sharpe_value = (
        float(sharpe) if isinstance(sharpe, int | float) and not isinstance(sharpe, bool) else None
    )
    max_dd_value = max_dd if isinstance(max_dd, int) and not isinstance(max_dd, bool) else None
    return sharpe_value, max_dd_value


def _resim_returns(
    entry: Mapping[str, object],
    *,
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    tf_minutes: int,
    start_equity_paisa: int,
) -> list[float]:
    """Re-simulate one entry over the trailing window → per-trade returns."""
    from brutex.engine.combo_mask import mask_to_entry_signals
    from brutex.engine.trade_sim import simulate_trades
    from brutex.strategies.types import Side

    mask = int(str(entry.get("mask_hex")), 16)
    sl_paisa, tp_paisa = _entry_arm(entry)
    side = Side.BUY if entry.get("direction") == "bull" else Side.SELL
    trades = simulate_trades(
        list(bars),
        mask_to_entry_signals(bar_bits, mask),
        direction=side,
        tf_minutes=tf_minutes,
        stop_loss_paisa=sl_paisa,
        take_profit_paisa=tp_paisa,
    )
    return [trade.pnl_paisa / start_equity_paisa for trade in trades]


def run_decay_leg(
    *,
    lake_root: Path,
    results_root: Path,
    alerter: SweepAlerter | None = None,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """Run the R48 decay leg — the report block (never raises past the wrapper).

    Returns the block::

        {"status": "ok" | "skipped", "run_id": ..., "window_sessions": N,
         "checked": n, "decayed": n, "abstained": n, "skipped_entries": n,
         "axis_defaults_assumed": true, "entries": [...], "diff_path": ...}

    Raises
    ------
    ValueError
        On junk env knobs (folded into ``env_error`` by the wrapper).
    """
    from brutex.data.catalog import Catalog
    from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA
    from brutex.engine.top100_artifact import load_top100

    sessions = resolve_trailing_sessions()
    min_trades = resolve_decay_min_trades()
    now = now_utc if now_utc is not None else datetime.now(tz=UTC)

    runs = discover_top100_runs(results_root, limit=1)
    if not runs:
        return {"status": "skipped", "reason": "no_top100_artifact"}
    run = runs[0]
    artifact = load_top100(run.runs_dir, run.run_id)
    if artifact is None:
        return {"status": "skipped", "reason": "top100_invalid", "run_id": run.run_id}

    window_start = now - timedelta(days=math.ceil(sessions * 7 / 5) + _CALENDAR_SLACK_DAYS)
    slice_cache: dict[tuple[str, str], tuple[list[CandleBar], list[int], int]] = {}
    entries_out: list[dict[str, object]] = []
    checked = decayed = abstained = skipped = 0

    catalog_path = lake_root / "catalog.duckdb"
    with Catalog.open(catalog_path) as catalog:
        for entry in artifact.entries:
            instrument = str(entry.get("instrument"))
            tf_label = str(entry.get("tf"))
            record: dict[str, object] = {
                "rank": entry.get("rank"),
                "run_id": entry.get("run_id"),
                "join_key": entry_join_key(entry),
                "instrument": instrument,
                "tf": tf_label,
                "direction": entry.get("direction"),
            }
            try:
                prepared = _prepare_slice(
                    slice_cache,
                    instrument=instrument,
                    tf_label=tf_label,
                    lake_root=lake_root,
                    catalog=catalog,
                    window_start=window_start,
                    now=now,
                    sessions=sessions,
                )
                if prepared is None:
                    record["verdict"] = "skipped"
                    record["reason"] = "out_of_scope_or_unknown_tf"
                    skipped += 1
                    entries_out.append(record)
                    continue
                bars, bar_bits, tf_minutes = prepared
                if not bars:
                    record["verdict"] = "skipped"
                    record["reason"] = "no_trailing_bars"
                    skipped += 1
                    entries_out.append(record)
                    continue
                returns = _resim_returns(
                    entry,
                    bars=bars,
                    bar_bits=bar_bits,
                    tf_minutes=tf_minutes,
                    start_equity_paisa=DEFAULT_START_EQUITY_PAISA,
                )
                fold = trailing_fold(returns)
                discovery_sharpe, discovery_dd_paisa = _discovery_metrics(entry)
                record["trailing"] = {
                    "n_trades": fold.n_trades,
                    "sharpe": fold.sharpe,
                    "max_drawdown": fold.max_drawdown,
                    "net": fold.net,
                }
                if discovery_sharpe is None or discovery_dd_paisa is None:
                    verdict = "abstain_no_discovery"
                else:
                    verdict = decay_verdict_token(
                        trailing_sharpe=fold.sharpe,
                        discovery_sharpe=discovery_sharpe,
                        trailing_max_dd=fold.max_drawdown,
                        discovery_max_dd=discovery_dd_paisa / DEFAULT_START_EQUITY_PAISA,
                        n_trades=fold.n_trades,
                        min_trades=min_trades,
                    )
                record["verdict"] = verdict
                checked += 1
                if verdict == VERDICT_DECAYED:
                    decayed += 1
                    record["retire_flag"] = True
                elif verdict != "healthy":
                    abstained += 1
            except Exception as exc:
                logger.warning(
                    "nightly.decay.entry_error",
                    rank=entry.get("rank"),
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                record["verdict"] = "skipped"
                record["reason"] = f"entry_error:{type(exc).__name__}"
                skipped += 1
            entries_out.append(record)

    block: dict[str, object] = {
        "status": "ok",
        "run_id": run.run_id,
        "window_sessions": sessions,
        "min_trades": min_trades,
        "checked": checked,
        "decayed": decayed,
        "abstained": abstained,
        "skipped_entries": skipped,
        "axis_defaults_assumed": True,
        "entries": entries_out,
    }
    diff_path = run.runs_dir / run.run_id / DECAY_DIFF_FILENAME
    payload = {
        "schema": "brutex.nightly.decay_diff",
        "schema_version": 1,
        "generated_utc": now.isoformat(),
        **block,
    }
    rendered = json.dumps(payload, indent=2, sort_keys=False, allow_nan=False)
    try:
        atomic_write_simple(diff_path, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
        block["diff_path"] = str(diff_path)
    except OSError as exc:
        logger.warning("nightly.decay.diff_write_failed", path=str(diff_path), error=str(exc))
        block["diff_path"] = None
    if decayed > 0 and alerter is not None:
        worst = [r for r in entries_out if r.get("verdict") == VERDICT_DECAYED][:3]
        detail = "; ".join(
            f"rank {r['rank']} {r['instrument']} {r['tf']} {r['direction']}" for r in worst
        )
        with contextlib.suppress(Exception):
            alerter.strategy_decayed(
                run_id=run.run_id, decayed=decayed, checked=checked, detail=detail
            )
    logger.info(
        "nightly.decay.done",
        run_id=run.run_id,
        checked=checked,
        decayed=decayed,
        abstained=abstained,
        skipped=skipped,
    )
    return block


def _prepare_slice(
    cache: dict[tuple[str, str], tuple[list[CandleBar], list[int], int]],
    *,
    instrument: str,
    tf_label: str,
    lake_root: Path,
    catalog: Catalog,
    window_start: datetime,
    now: datetime,
    sessions: int,
) -> tuple[list[CandleBar], list[int], int] | None:
    """Load + trim + Stage-1A one ``(instrument, tf)`` slice (memoised).

    ``None`` = out-of-scope instrument / unknown tf label — the caller
    SKIPS the entry honestly (counted, never guessed).
    """
    from brutex.data.timeframes import spec_for_lake_label
    from brutex.engine.lake_slice import load_lake_slice
    from brutex.engine.stage1a import build_bar_bits

    key = (instrument, tf_label)
    if key in cache:
        return cache[key]
    coords = instrument_coords(instrument)
    if coords is None:
        return None
    try:
        tf_minutes = spec_for_lake_label(tf_label).minutes
    except ValueError:
        return None
    exchange, segment = coords
    bars = load_lake_slice(
        lake_root=lake_root,
        catalog=catalog,
        exchange=exchange,
        segment=segment,
        symbol=instrument,
        interval=tf_label,
        start=window_start,
        end=now,
        dedup_source=f"nightly.decay:{instrument}/{tf_label}",
    )
    trailing = trailing_session_bars(bars, sessions)
    bar_bits: list[int] = build_bar_bits(trailing) if trailing else []
    prepared = (trailing, bar_bits, tf_minutes)
    cache[key] = prepared
    return prepared


def run_decay_leg_guarded(
    *,
    lake_root: Path,
    results_root: Path,
    alerter: SweepAlerter | None = None,
    now_utc: datetime | None = None,
) -> dict[str, object]:
    """The fail-open wrapper ``run.py`` binds (the #1939 report-block law).

    Never raises: a junk env knob reads ``{"status": "env_error"}``, a
    disabled flag ``{"status": "disabled"}``, any other failure
    ``{"status": "error", ...}`` — the chain is NEVER broken by this leg.
    """
    try:
        if not nightly_decay_enabled():
            return {"status": "disabled"}
    except ValueError as exc:
        logger.warning("nightly.decay.env_error", error=str(exc))
        return {"status": "env_error", "error": str(exc)[:200]}
    try:
        return run_decay_leg(
            lake_root=lake_root, results_root=results_root, alerter=alerter, now_utc=now_utc
        )
    except ValueError as exc:
        logger.warning("nightly.decay.env_error", error=str(exc))
        return {"status": "env_error", "error": str(exc)[:200]}
    except Exception as exc:
        logger.error("nightly.decay.leg_error", error_type=type(exc).__name__, error=str(exc)[:200])
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:200]}
