"""Champion/challenger promotion ledger + nightly run-delta (LANE W5).

DEC-CHAMPION-CHALLENGER-001 + DEC-NIGHTLY-RUN-DELTA-001. The second
report-level adaptation leg (the #1939 pattern — fail-open, NEVER a chain
link): per ``(instrument, tf, direction)`` key the nightly maintains a
DURABLE champion record, challenges it with tonight's best
cascade-PASSING ``top100.json`` entry, and PROMOTES a challenger only
after :data:`CHAMPION_PROMOTE_NIGHTS` CONSECUTIVE dominating nights —
every transition appended to a hash-chained JSONL ledger
(:class:`~brutex.orchestrator.audit.AuditWriter`, actor ``"champions"`` —
Golden Rule #8).

The domination law (per night)
------------------------------
1. **Eligibility**: the challenger must be a cascade ``"pass"`` entry in
   TONIGHT's artifact (non-provisional — a pool-N provisional pass never
   challenges).
2. **Pooled re-validation**: champion + challenger are re-simulated into
   one POOLED Stage-4 cascade re-run
   (:func:`~brutex.engine.cascade_wire.build_candidate_input` →
   :func:`~brutex.metrics.cascade.run_stage4_cascade` over the shared
   bars/bar_bits) — the challenger must SURVIVE it non-provisionally.
3. **Domination**: the challenger's measured LOCKBOX Sharpe must be
   STRICTLY higher with a lockbox max-DD no worse — the born-Rust
   :func:`~brutex.nightly.decay_math.champion_dominates` kernel over
   :func:`~brutex.metrics.cpcv.sharpe_of_returns` /
   the kernel fold of each ``CandidateInput.lockbox_returns``.

A different challenger identity RESETS the streak (consecutive means the
SAME challenger). RETIREMENT is an ANNOTATION only (era-tagged
``retire_flag`` — from the decay leg's DECAYED verdict on the champion's
join identity); a champion record is NEVER deleted — promotion moves the
old champion into the era-tagged ``former`` history (R13: kept forever).

run-delta.json (DEC-NIGHTLY-RUN-DELTA-001)
------------------------------------------
The nightly COMPARE substrate between the prior night's artifact and
tonight's, joined on :func:`~brutex.nightly.decay.entry_join_key` — the
Rule-4 identity fields MINUS ``data_snapshot_hash`` + ``code_commit_sha``
(which re-key nightly, so a ``run_id`` join would never match):
``entered`` / ``dropped`` / ``rank_moves`` + headline metric deltas.
Written atomically into tonight's run dir.

Honest envelope (Golden Rule #10)
---------------------------------
* The pooled re-run costs ``O(window_bars)`` per challenged key (bars +
  Stage-1A + 2 mask re-sims + one 2-candidate cascade) — the same class
  as ONE sweep tuple; trivial beside the sweep. Never O(1)-total.
* The top100 entries do not carry non-default Stage-1A axes — the pooled
  re-run evaluates the DEFAULT cell (disclosed, ``axis_defaults_assumed``).
* State/ledger writes are O(1) per night; the run-delta join is O(~100).
* Diff/ledger orchestration here is PYTHON over the Rust verdict kernels —
  the born-Rust law covers the VERDICT/FOLD math; the I/O glue is
  Rust-optional (disclosed in the ledger entry).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.nightly.decay import discover_top100_runs, entry_join_key, instrument_coords
from brutex.nightly.decay_math import champion_dominates, trailing_fold

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brutex.data.models import CandleBar
    from brutex.engine.top100_artifact import Top100Artifact
    from brutex.notifications.sweep_alerts import SweepAlerter

logger = structlog.get_logger(__name__)

__all__ = [
    "CHAMPIONS_DIRNAME",
    "CHAMPIONS_ENABLED_DEFAULT",
    "CHAMPIONS_ENABLED_ENV",
    "CHAMPION_PROMOTE_NIGHTS",
    "CHAMPION_PROMOTE_NIGHTS_ENV",
    "LEDGER_FILENAME",
    "RUN_DELTA_FILENAME",
    "STATE_FILENAME",
    "build_run_delta",
    "challenger_eligible",
    "champion_scope_key",
    "load_champions_state",
    "nightly_champions_enabled",
    "resolve_promote_nights",
    "run_champions_leg",
    "run_champions_leg_guarded",
    "save_champions_state",
]

#: The leg's kill-switch env flag (fail-loud tristate — the decay twin).
CHAMPIONS_ENABLED_ENV: Final[str] = "BRUTEX_NIGHTLY_CHAMPIONS"

#: Default ON (flipped in-branch once the transition golden was green).
CHAMPIONS_ENABLED_DEFAULT: Final[bool] = True

#: Consecutive dominating nights required before a promotion (a W5-default
#: named constant standing in for an un-ruled operator value — disclosed).
CHAMPION_PROMOTE_NIGHTS: Final[int] = 3

#: Fail-loud env override of :data:`CHAMPION_PROMOTE_NIGHTS`.
CHAMPION_PROMOTE_NIGHTS_ENV: Final[str] = "BRUTEX_CHAMPION_PROMOTE_NIGHTS"

#: Durable state layout under ``<results_root>/champions/``.
CHAMPIONS_DIRNAME: Final[str] = "champions"
STATE_FILENAME: Final[str] = "state.json"
LEDGER_FILENAME: Final[str] = "champions.jsonl"

#: The per-run compare sidecar written into tonight's run dir.
RUN_DELTA_FILENAME: Final[str] = "run-delta.json"

#: state.json schema version.
_STATE_SCHEMA_VERSION: Final[int] = 1

#: Ledger action vocabulary (Golden Rule #8 — every transition appended).
_ACTION_SEEDED: Final[str] = "SEEDED"
_ACTION_CHALLENGED: Final[str] = "CHALLENGED"
_ACTION_PROMOTED: Final[str] = "PROMOTED"
_ACTION_RETAINED: Final[str] = "RETAINED"
_ACTION_RETIRE_FLAGGED: Final[str] = "RETIRE_FLAGGED"


def nightly_champions_enabled() -> bool:
    """Resolve ``BRUTEX_NIGHTLY_CHAMPIONS`` (fail-loud tristate)."""
    import os

    raw = os.environ.get(CHAMPIONS_ENABLED_ENV)
    if raw is None or not raw.strip():
        return CHAMPIONS_ENABLED_DEFAULT
    verdict = env_flag_tristate(raw)
    if verdict is None:
        raise ValueError(
            f"{CHAMPIONS_ENABLED_ENV}={raw!r} is not a recognised flag literal "
            "(use 1/true/yes/on or 0/false/no/off)"
        )
    return verdict


def resolve_promote_nights() -> int:
    """Resolve the promotion streak floor (env override, fail-loud on junk)."""
    import os

    raw = os.environ.get(CHAMPION_PROMOTE_NIGHTS_ENV)
    if raw is None or not raw.strip():
        return CHAMPION_PROMOTE_NIGHTS
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(
            f"{CHAMPION_PROMOTE_NIGHTS_ENV}={raw!r} is not an integer — set a whole number >= 1"
        ) from exc
    if value < 1:
        raise ValueError(f"{CHAMPION_PROMOTE_NIGHTS_ENV}={raw!r} must be >= 1")
    return value


def champion_scope_key(entry: Mapping[str, object]) -> str:
    """The champion SCOPE key: ``instrument/tf/direction`` (the task law).

    The separator is ``"/"`` (never ``"|"``) because the scope key is
    embedded in the hash-chained ledger's ``subject`` field, whose
    preimage encoding forbids the ``"|"`` delimiter byte
    (:class:`brutex.orchestrator.audit.AuditEntry`).
    """
    return "/".join(
        (str(entry.get("instrument")), str(entry.get("tf")), str(entry.get("direction")))
    )


def challenger_eligible(entry: Mapping[str, object]) -> bool:
    """True iff the entry carries a NON-provisional cascade ``"pass"``."""
    cascade = entry.get("cascade")
    if not isinstance(cascade, Mapping):
        return False
    return cascade.get("status") == "pass"


# ── durable state ───────────────────────────────────────────────────────


def _champions_dir(results_root: Path) -> Path:
    return results_root / CHAMPIONS_DIRNAME


def load_champions_state(results_root: Path) -> dict[str, object]:
    """Load ``champions/state.json`` (absent/corrupt → a fresh state + WARN)."""
    path = _champions_dir(results_root) / STATE_FILENAME
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {"schema_version": _STATE_SCHEMA_VERSION, "champions": {}}
    try:
        state = json.loads(raw)
    except ValueError:
        logger.warning("nightly.champions.state_corrupt", path=str(path))
        return {"schema_version": _STATE_SCHEMA_VERSION, "champions": {}}
    if not isinstance(state, dict) or not isinstance(state.get("champions"), dict):
        logger.warning("nightly.champions.state_bad_shape", path=str(path))
        return {"schema_version": _STATE_SCHEMA_VERSION, "champions": {}}
    return state


def save_champions_state(results_root: Path, state: Mapping[str, object]) -> Path:
    """Atomically persist ``champions/state.json``; returns the path."""
    directory = _champions_dir(results_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILENAME
    rendered = json.dumps(state, indent=2, sort_keys=True, allow_nan=False)
    atomic_write_simple(path, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    return path


# ── run-delta ───────────────────────────────────────────────────────────


def _entry_headline(entry: Mapping[str, object]) -> dict[str, object]:
    """The compact per-entry headline carried in run-delta rows."""
    metrics = entry.get("metrics")
    metrics_map: Mapping[str, object] = metrics if isinstance(metrics, Mapping) else {}
    return {
        "rank": entry.get("rank"),
        "join_key": entry_join_key(entry),
        "instrument": entry.get("instrument"),
        "tf": entry.get("tf"),
        "direction": entry.get("direction"),
        "net_pnl_paisa": metrics_map.get("net_pnl_paisa"),
        "sharpe_ratio": metrics_map.get("sharpe_ratio"),
    }


def build_run_delta(prior: Top100Artifact | None, current: Top100Artifact) -> dict[str, object]:
    """The entered/dropped/rank-shift compare, joined on the snapshot-free key."""
    current_by_key = {entry_join_key(e): e for e in current.entries}
    if prior is None:
        return {
            "status": "no_prior",
            "current_run_id": current.run_id,
            "prior_run_id": None,
            "entered": [_entry_headline(e) for e in current.entries],
            "dropped": [],
            "rank_moves": [],
            "common": 0,
        }
    prior_by_key = {entry_join_key(e): e for e in prior.entries}
    entered = [_entry_headline(e) for key, e in current_by_key.items() if key not in prior_by_key]
    dropped = [_entry_headline(e) for key, e in prior_by_key.items() if key not in current_by_key]
    rank_moves: list[dict[str, object]] = []
    common = 0
    for key, cur in current_by_key.items():
        old = prior_by_key.get(key)
        if old is None:
            continue
        common += 1
        if old.get("rank") != cur.get("rank"):
            rank_moves.append(
                {"join_key": key, "from_rank": old.get("rank"), "to_rank": cur.get("rank")}
            )
    return {
        "status": "ok",
        "current_run_id": current.run_id,
        "prior_run_id": prior.run_id,
        "entered": entered,
        "dropped": dropped,
        "rank_moves": rank_moves,
        "common": common,
    }


# ── pooled re-validation ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _PooledVerdict:
    """The pooled cascade re-run's outcome for one challenge."""

    challenger_survives: bool
    challenger_provisional: bool
    dominates: bool
    challenger_lockbox_sharpe: float | None
    champion_lockbox_sharpe: float | None
    challenger_lockbox_mdd: float | None
    champion_lockbox_mdd: float | None
    detail: str


def _candidate_spec(record: Mapping[str, object]) -> tuple[int, str, int | None, int | None]:
    """``(mask, direction, sl, tp)`` from a champion record / entry-shaped map."""
    mask = int(str(record.get("mask_hex")), 16)
    direction = str(record.get("direction"))
    sl = record.get("sl_paisa")
    tp = record.get("tp_paisa")
    return (
        mask,
        direction,
        sl if isinstance(sl, int) else None,
        tp if isinstance(tp, int) else None,
    )


def _lockbox_metrics(lockbox_returns: Sequence[float]) -> tuple[float | None, float]:
    """``(sharpe, max_dd)`` of one candidate's lockbox series (kernel fold)."""
    fold = trailing_fold(lockbox_returns)
    return fold.sharpe, fold.max_drawdown


def _pooled_check(
    *,
    champion: Mapping[str, object],
    challenger: Mapping[str, object],
    bars: Sequence[CandleBar],
    bar_bits: Sequence[int],
    tf_minutes: int,
    sweep_run_id: str,
) -> _PooledVerdict:
    """Pooled {champion ∪ challenger} Stage-4 cascade re-run + domination."""
    from brutex.engine.cascade_wire import build_candidate_input, cascade_config_for_sweep
    from brutex.engine.sweep_driver import DEFAULT_START_EQUITY_PAISA
    from brutex.metrics.cascade import run_stage4_cascade

    config = cascade_config_for_sweep(sweep_run_id)
    candidates = []
    for candidate_id, record in (("champion", champion), ("challenger", challenger)):
        mask, direction, sl, tp = _candidate_spec(record)
        candidates.append(
            build_candidate_input(
                run_id=candidate_id,
                mask=mask,
                bars=list(bars),
                bar_bits=list(bar_bits),
                direction_label=direction,
                tf_minutes=tf_minutes,
                sl_paisa=sl,
                tp_paisa=tp,
                start_equity_paisa=DEFAULT_START_EQUITY_PAISA,
            )
        )
    champion_ci, challenger_ci = candidates
    report = run_stage4_cascade(candidates, config)
    challenger_id = challenger_ci.candidate_id
    survives = challenger_id in report.survivor_ids
    provisional = challenger_id in report.provisional_pass_ids
    chall_sharpe, chall_mdd = _lockbox_metrics(challenger_ci.lockbox_returns)
    champ_sharpe, champ_mdd = _lockbox_metrics(champion_ci.lockbox_returns)
    dominates = False
    detail = ""
    if chall_sharpe is None or champ_sharpe is None:
        detail = "lockbox sharpe undefined on one side — no domination claim"
    else:
        dominates = champion_dominates(
            challenger_sharpe=chall_sharpe,
            challenger_max_dd=chall_mdd,
            champion_sharpe=champ_sharpe,
            champion_max_dd=champ_mdd,
        )
        detail = (
            f"lockbox sharpe {chall_sharpe:.4f} vs {champ_sharpe:.4f}, "
            f"mdd {chall_mdd:.6f} vs {champ_mdd:.6f}"
        )
    return _PooledVerdict(
        challenger_survives=survives,
        challenger_provisional=provisional,
        dominates=dominates,
        challenger_lockbox_sharpe=chall_sharpe,
        champion_lockbox_sharpe=champ_sharpe,
        challenger_lockbox_mdd=chall_mdd,
        champion_lockbox_mdd=champ_mdd,
        detail=detail,
    )


# ── the leg ─────────────────────────────────────────────────────────────


def _champion_record_from_entry(
    entry: Mapping[str, object], *, sweep_run_id: str, now_iso: str, era: int
) -> dict[str, object]:
    """Mint a champion record from a top100 entry (annotation-shaped)."""
    metrics = entry.get("metrics")
    metrics_map: Mapping[str, object] = metrics if isinstance(metrics, Mapping) else {}
    arm = entry.get("arm")
    arm_map: Mapping[str, object] = arm if isinstance(arm, Mapping) else {}
    return {
        "era": era,
        "join_key": entry_join_key(entry),
        "run_id": entry.get("run_id"),
        "rank": entry.get("rank"),
        "mask_hex": entry.get("mask_hex"),
        "instrument": entry.get("instrument"),
        "tf": entry.get("tf"),
        "direction": entry.get("direction"),
        "sl_paisa": arm_map.get("sl_paisa"),
        "tp_paisa": arm_map.get("tp_paisa"),
        "source_sweep_run_id": sweep_run_id,
        "seeded_utc": now_iso,
        "discovery_sharpe": metrics_map.get("sharpe_ratio"),
        "discovery_max_dd_paisa": metrics_map.get("max_drawdown_paisa"),
        "retire_flag": None,
        "challenger": None,
        "former": [],
    }


def _decayed_join_keys(decay_block: Mapping[str, object] | None) -> frozenset[str]:
    """Join keys the decay leg flagged DECAYED tonight (empty when absent)."""
    if not isinstance(decay_block, Mapping):
        return frozenset()
    entries = decay_block.get("entries")
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        str(record.get("join_key"))
        for record in entries
        if isinstance(record, Mapping) and record.get("verdict") == "decayed"
    )


def run_champions_leg(
    *,
    lake_root: Path,
    results_root: Path,
    decay_block: Mapping[str, object] | None = None,
    alerter: SweepAlerter | None = None,
    now_utc: datetime | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, object]:
    """Run the champion/challenger + run-delta leg — the report block.

    Raises
    ------
    ValueError
        On junk env knobs (folded into ``env_error`` by the wrapper).
    """
    from brutex.engine.top100_artifact import load_top100
    from brutex.orchestrator.audit import AuditWriter

    promote_nights = resolve_promote_nights()
    now = now_utc if now_utc is not None else datetime.now(tz=UTC)
    now_iso = now.isoformat()

    runs = discover_top100_runs(results_root, limit=2)
    if not runs:
        return {"status": "skipped", "reason": "no_top100_artifact"}
    tonight = runs[0]
    current = load_top100(tonight.runs_dir, tonight.run_id)
    if current is None:
        return {"status": "skipped", "reason": "top100_invalid", "run_id": tonight.run_id}
    prior = load_top100(runs[1].runs_dir, runs[1].run_id) if len(runs) > 1 else None

    # ── run-delta (DEC-NIGHTLY-RUN-DELTA-001) ──
    delta = build_run_delta(prior, current)
    delta_path = tonight.runs_dir / tonight.run_id / RUN_DELTA_FILENAME
    delta_payload = {
        "schema": "brutex.nightly.run_delta",
        "schema_version": 1,
        "generated_utc": now_iso,
        **delta,
    }
    rendered = json.dumps(delta_payload, indent=2, sort_keys=False, allow_nan=False)
    try:
        atomic_write_simple(delta_path, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
        delta_summary: dict[str, object] = {
            "status": delta["status"],
            "entered": len(delta["entered"]),  # type: ignore[arg-type]
            "dropped": len(delta["dropped"]),  # type: ignore[arg-type]
            "rank_moves": len(delta["rank_moves"]),  # type: ignore[arg-type]
            "common": delta["common"],
            "path": str(delta_path),
        }
    except OSError as exc:
        logger.warning("nightly.champions.delta_write_failed", path=str(delta_path), error=str(exc))
        delta_summary = {"status": "write_failed", "error": str(exc)[:200]}

    # ── champion transitions ──
    state = load_champions_state(results_root)
    champions_raw = state.get("champions")
    champions: dict[str, object] = champions_raw if isinstance(champions_raw, dict) else {}
    state["champions"] = champions
    decayed_keys = _decayed_join_keys(decay_block)

    # Group tonight's entries by scope key, rank order preserved.
    by_scope: dict[str, list[Mapping[str, object]]] = {}
    for entry in current.entries:
        by_scope.setdefault(champion_scope_key(entry), []).append(entry)

    transitions: list[dict[str, object]] = []
    ledger_dir = _champions_dir(results_root)
    ledger_dir.mkdir(parents=True, exist_ok=True)
    with AuditWriter.open(ledger_dir / LEDGER_FILENAME, actor="champions", resume=True) as ledger:

        def _record(action: str, scope: str, payload: dict[str, object]) -> None:
            ledger.append(action=action, subject=f"champion:{scope}", payload=payload)
            transitions.append({"action": action, "scope": scope, **payload})

        for scope in sorted(by_scope):
            entries = by_scope[scope]
            eligible = [e for e in entries if challenger_eligible(e)]
            champ = champions.get(scope)
            if champ is None:
                if not eligible:
                    continue  # no champion, nothing eligible — nothing to record
                record = _champion_record_from_entry(
                    eligible[0], sweep_run_id=current.run_id, now_iso=now_iso, era=1
                )
                champions[scope] = record
                _record(
                    _ACTION_SEEDED,
                    scope,
                    {"join_key": record["join_key"], "run_id": record["run_id"], "era": 1},
                )
                continue
            if not isinstance(champ, dict):
                # A hand-corrupted record: refuse to judge over junk — reseed
                # honestly next night after the operator inspects the state.
                logger.warning("nightly.champions.record_corrupt", scope=scope)
                continue
            # Decay retire-FLAG (annotation only — never a deletion).
            if str(champ.get("join_key")) in decayed_keys and champ.get("retire_flag") is None:
                champ["retire_flag"] = {"reason": "decayed", "flagged_utc": now_iso}
                _record(
                    _ACTION_RETIRE_FLAGGED,
                    scope,
                    {"join_key": champ["join_key"], "reason": "decayed"},
                )
            challenger = next(
                (e for e in eligible if entry_join_key(e) != str(champ.get("join_key"))), None
            )
            if challenger is None:
                champ["challenger"] = None
                _record(
                    _ACTION_RETAINED,
                    scope,
                    {"join_key": champ["join_key"], "reason": "no_challenger"},
                )
                continue
            verdict = _challenge(
                champ,
                challenger,
                lake_root=lake_root,
                sweep_run_id=current.run_id,
                from_date=from_date,
                to_date=to_date,
                now=now,
            )
            if verdict is None or not (
                verdict.challenger_survives
                and not verdict.challenger_provisional
                and verdict.dominates
            ):
                champ["challenger"] = None
                _record(
                    _ACTION_RETAINED,
                    scope,
                    {
                        "join_key": champ["join_key"],
                        "reason": "challenger_did_not_dominate"
                        if verdict is not None
                        else "pooled_check_failed",
                        "detail": verdict.detail if verdict is not None else "",
                    },
                )
                continue
            challenger_key = entry_join_key(challenger)
            streak_state = champ.get("challenger")
            streak = 1
            if (
                isinstance(streak_state, dict)
                and streak_state.get("join_key") == challenger_key
                and isinstance(streak_state.get("streak"), int)
            ):
                streak = int(streak_state["streak"]) + 1
            if streak >= promote_nights:
                former = champ.get("former")
                former_list = former if isinstance(former, list) else []
                era = int(champ.get("era", 1)) + 1
                retired = {k: v for k, v in champ.items() if k != "former"}
                retired["retired_utc"] = now_iso
                retired["retired_reason"] = "superseded_by_promotion"
                new_record = _champion_record_from_entry(
                    challenger, sweep_run_id=current.run_id, now_iso=now_iso, era=era
                )
                new_record["former"] = [*former_list, retired]
                champions[scope] = new_record
                _record(
                    _ACTION_PROMOTED,
                    scope,
                    {
                        "join_key": challenger_key,
                        "run_id": new_record["run_id"],
                        "era": era,
                        "streak": streak,
                        "detail": verdict.detail,
                    },
                )
                if alerter is not None:
                    with contextlib.suppress(Exception):
                        alerter.champion_promoted(
                            scope=scope,
                            run_id=str(new_record["run_id"]),
                            rank=int(str(challenger.get("rank"))),
                            detail=verdict.detail,
                        )
            else:
                champ["challenger"] = {
                    "join_key": challenger_key,
                    "streak": streak,
                    "last_night_utc": now_iso,
                }
                _record(
                    _ACTION_CHALLENGED,
                    scope,
                    {
                        "join_key": challenger_key,
                        "streak": streak,
                        "needed": promote_nights,
                        "detail": verdict.detail,
                    },
                )

    state["schema_version"] = _STATE_SCHEMA_VERSION
    state_path = save_champions_state(results_root, state)
    block: dict[str, object] = {
        "status": "ok",
        "run_id": current.run_id,
        "promote_nights": promote_nights,
        "champions": len(champions),
        "transitions": transitions,
        "run_delta": delta_summary,
        "state_path": str(state_path),
    }
    logger.info(
        "nightly.champions.done",
        run_id=current.run_id,
        champions=len(champions),
        transitions=len(transitions),
    )
    return block


def _challenge(
    champ: Mapping[str, object],
    challenger: Mapping[str, object],
    *,
    lake_root: Path,
    sweep_run_id: str,
    from_date: str | None,
    to_date: str | None,
    now: datetime,
) -> _PooledVerdict | None:
    """Load the scope's bars + run the pooled check (``None`` on any fault)."""
    from brutex.data.catalog import Catalog
    from brutex.data.timeframes import spec_for_lake_label
    from brutex.engine.lake_slice import load_lake_slice
    from brutex.engine.stage1a import build_bar_bits

    instrument = str(challenger.get("instrument"))
    tf_label = str(challenger.get("tf"))
    coords = instrument_coords(instrument)
    if coords is None:
        return None
    try:
        tf_minutes = spec_for_lake_label(tf_label).minutes
    except ValueError:
        return None
    start, end = _window_bounds(from_date, to_date, now=now)
    try:
        with Catalog.open(lake_root / "catalog.duckdb") as catalog:
            bars = load_lake_slice(
                lake_root=lake_root,
                catalog=catalog,
                exchange=coords[0],
                segment=coords[1],
                symbol=instrument,
                interval=tf_label,
                start=start,
                end=end,
                dedup_source=f"nightly.champions:{instrument}/{tf_label}",
            )
        if not bars:
            return None
        bar_bits = build_bar_bits(bars)
        challenger_arm = challenger.get("arm")
        challenger_map: dict[str, object] = dict(challenger)
        if isinstance(challenger_arm, Mapping):
            challenger_map["sl_paisa"] = challenger_arm.get("sl_paisa")
            challenger_map["tp_paisa"] = challenger_arm.get("tp_paisa")
        return _pooled_check(
            champion=champ,
            challenger=challenger_map,
            bars=bars,
            bar_bits=bar_bits,
            tf_minutes=tf_minutes,
            sweep_run_id=sweep_run_id,
        )
    except Exception as exc:
        logger.warning(
            "nightly.champions.pooled_check_failed",
            instrument=instrument,
            tf=tf_label,
            error_type=type(exc).__name__,
            error=str(exc)[:200],
        )
        return None


def _window_bounds(
    from_date: str | None, to_date: str | None, *, now: datetime
) -> tuple[datetime, datetime]:
    """The pooled re-run's bar window (the plan dates; floor→now default)."""
    from brutex.dashboard.pull_controller import GROWW_HISTORY_FLOOR

    if from_date:
        start = datetime.fromisoformat(from_date).replace(tzinfo=UTC)
    else:
        floor = GROWW_HISTORY_FLOOR  # the ONE Track-1 pull-floor constant
        start = datetime(floor.year, floor.month, floor.day, tzinfo=UTC)
    if to_date:
        end = datetime.fromisoformat(to_date).replace(tzinfo=UTC) + timedelta(days=1)
    else:
        end = now
    return start, end


def run_champions_leg_guarded(
    *,
    lake_root: Path,
    results_root: Path,
    decay_block: Mapping[str, object] | None = None,
    alerter: SweepAlerter | None = None,
    now_utc: datetime | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, object]:
    """The fail-open wrapper ``run.py`` binds (the #1939 report-block law)."""
    try:
        if not nightly_champions_enabled():
            return {"status": "disabled"}
    except ValueError as exc:
        logger.warning("nightly.champions.env_error", error=str(exc))
        return {"status": "env_error", "error": str(exc)[:200]}
    try:
        return run_champions_leg(
            lake_root=lake_root,
            results_root=results_root,
            decay_block=decay_block,
            alerter=alerter,
            now_utc=now_utc,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        logger.warning("nightly.champions.env_error", error=str(exc))
        return {"status": "env_error", "error": str(exc)[:200]}
    except Exception as exc:
        logger.error(
            "nightly.champions.leg_error", error_type=type(exc).__name__, error=str(exc)[:200]
        )
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)[:200]}
