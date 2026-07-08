"""P4 integration adapter for the one-click sweep artifact tree (DEC-INSTITUTIONAL-ONECLICK-001).

This is the ONE module in the P4 (dashboard/display) zone that touches
sweep-run artifact I/O — every route in :mod:`brutex.dashboard.app` that
serves the Operator page's live progress, run history, Top-100 leaderboard,
trade list, timeline, and VERIFY-ME panel goes through a function here.
Nothing else in this PR does artifact I/O (see
``sweep-parallel-integration-contracts`` / ``sweep-gap-register-jul8``,
Ruling 4).

## ADAPTER ROOT RESOLUTION LAW (binding)

:func:`sweep_runs_root` is the single source of truth for where run
artifacts live: ``$BRUTEX_ONECLICK_FIXTURE_ROOT`` when set (test/dev
override — that directory itself IS the ``sweep_runs``-equivalent root,
holding ``<run_id>/`` subdirectories directly), else
``<lake_root>/sweep_runs`` in production (Ruling 4 — a SIBLING root next to
the existing ``.parquet``-globbed sweeps tree, never inside it; ``lake_root``
is the exact :class:`~pathlib.Path` already resolved elsewhere in
:mod:`brutex.dashboard.app` as ``store.path.parent`` for
``GET /api/v1/db/candles``). Every other function in this module calls
:func:`sweep_runs_root` exactly once, so a future root relocation (e.g. P2
renaming ``sweep_runs/``) is a one-line change here. It never raises even
when the directory does not exist yet — "no run has ever launched" is a
valid state, not an error.

## Honest-absence discipline

P1/P2/P3 (the preflight/orchestrator/results-depth phases) have not merged
into this branch yet. Every reader below therefore returns an explicit
sentinel — ``{"state": "awaiting_artifact", ...}`` — instead of raising or
fabricating data when a run directory or a specific artifact file does not
exist. A file that EXISTS but fails to parse is a **different**, louder
state — ``{"state": "artifact_error", ...}`` — never conflated with honest
absence (see :func:`read_json_artifact`). Routes render a loud "awaiting
real artifact" banner for the former and a loud "artifact present but
unreadable" banner for the latter; neither is ever a blank or
fake-populated page.

## Path-traversal defence

Every reader that takes a caller-supplied ``run_id`` calls
:func:`validate_run_id` FIRST, before any :class:`~pathlib.Path` join. A
``run_id`` that fails the allowlist regex raises :class:`ValueError`, which
routes translate to HTTP 400.

## O(1) vs honestly-not-O(1)

:func:`read_chain_map`, :func:`read_exit_status`, :func:`read_manifest`,
:func:`read_top100`, and :func:`read_verify_me` are each ONE small atomic
JSON read — O(1) in the number of runs/results. :func:`list_runs` is
O(number of run directories) capped by ``limit`` — documented, never
falsely claimed O(1) (universal-engineering-standards). :func:`iter_timeline`
is a cheap bounded cursor read by default, and an HONESTLY bounded (not
O(1)) reverse text/byte scan when ``q`` is supplied — the ``bounded_scan``
flag in its return value makes the distinction visible to callers, and the
UI is required to invoke ``q`` only from an explicit search action, never
the auto-refresh poll timer.

## Reuse discipline (CLAUDE.md §11 COMMON)

:func:`read_top100` and :func:`read_trades_page` decorate rows with dual
Simple/Pro sentences by calling :func:`brutex.dashboard.combo_decoder.decode_combo`
verbatim — no phrase table is rebuilt here.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from brutex.dashboard.combo_decoder import decode_combo

#: Test/dev override env var. When set, the directory it names IS the
#: ``sweep_runs``-equivalent root (holds ``<run_id>/`` subdirectories
#: directly) — see the ADAPTER ROOT RESOLUTION LAW above.
FIXTURE_ROOT_ENV: Final[str] = "BRUTEX_ONECLICK_FIXTURE_ROOT"

#: run_id allowlist — path-traversal defence (see module docstring). Letters,
#: digits, underscore, dot, colon, hyphen; 1..128 chars. No ``/`` or ``..``
#: can ever reach a Path join through a validated run_id.
_RUN_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")

#: The 7 R44 VERIFY-ME check names, in the fixed display order the operator
#: acceptance panel renders them.
VERIFY_ME_CHECKS: Final[tuple[str, ...]] = (
    "determinism",
    "audit_replay",
    "trade_chart_match",
    "plain_english_arithmetic",
    "kill_resume",
    "dedup_zero_recomputed",
    "funnel_reconcile",
)
_VERIFY_ME_STATUSES: Final[frozenset[str]] = frozenset({"pass", "fail", "awaiting"})

#: iter_timeline's ``q=`` search path bound (documented, never O(1)).
_TIMELINE_SCAN_MAX_BYTES: Final[int] = 2 * 1024 * 1024
_TIMELINE_SCAN_MAX_LINES: Final[int] = 5000

#: read_trades_page's server-side hard cap on ``limit``.
_TRADES_LIMIT_MAX: Final[int] = 500

ReadState = Literal["ok", "absent", "corrupt"]


# ─────────────────────────── root + run_id primitives ───────────────────────────


def sweep_runs_root(lake_root: Path) -> Path:
    """Resolve the sweep-runs artifact root per the ADAPTER ROOT RESOLUTION LAW.

    Returns ``$BRUTEX_ONECLICK_FIXTURE_ROOT`` (as a :class:`Path`) if that env
    var is set and non-empty — the test/dev override, where the named
    directory itself contains ``<run_id>/`` subdirectories directly — else
    ``lake_root / "sweep_runs"`` (Ruling 4's production sibling root).

    Never raises, never touches the filesystem — a pure path computation.
    "The directory does not exist yet" (no run has ever launched) is a valid,
    expected state; callers detect it via :func:`read_json_artifact` /
    :func:`list_runs`'s own ``FileNotFoundError`` handling, not here.
    """
    override = os.environ.get(FIXTURE_ROOT_ENV)
    if override:
        return Path(override)
    return lake_root / "sweep_runs"


def validate_run_id(run_id: str) -> str:
    """Return *run_id* unchanged if it is filesystem-safe, else raise ``ValueError``.

    A ``run_id`` reaches the filesystem via a :class:`Path` join in every
    reader below, so this allowlist (letters/digits/``_``/``.``/``:``/``-``,
    1..128 chars — no ``/``, no null bytes) is the ONE centralised
    path-traversal defence. The allowlist alone permits ``.``/``..`` as a
    *character* in a longer name (e.g. ``"run.v2"``), but the pattern
    contains no ``/`` so a MULTI-segment traversal (``"../../etc/passwd"``)
    can never match; the two single-segment special cases the OS still
    special-cases — the literal strings ``"."`` and ``".."`` — are rejected
    explicitly. Routes catch :class:`ValueError` here and return HTTP 400;
    callers MUST call this before any filesystem access, not trust the
    caller to have done so already.
    """
    if not isinstance(run_id, str) or not _RUN_ID_RE.match(run_id) or run_id in (".", ".."):
        raise ValueError("invalid run_id")
    return run_id


def read_json_artifact(path: Path) -> tuple[ReadState, Any, str | None]:
    """Safely read one JSON artifact file — the ONE shared safe-read primitive.

    Returns ``("absent", None, None)`` when the file does not exist (honest
    "not yet written" state — one retry on the initial
    :class:`FileNotFoundError` to tolerate a concurrent atomic-rename-in-flight
    write, so a live poll never sees a false ``"corrupt"``). Returns
    ``("corrupt", None, "<message>")`` when the file exists but is not valid
    JSON, or any other :class:`OSError` occurs while reading it (permission
    error, is-a-directory, etc.) — a LOUD, distinct state from absence, per
    the ADAPTER ROOT RESOLUTION LAW. Returns ``("ok", data, None)`` on
    success. Never raises.
    """
    saw_not_found = False
    for attempt in range(2):
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            saw_not_found = True
            if attempt == 0:
                continue
            return ("absent", None, None)
        except OSError as exc:
            return ("corrupt", None, f"{type(exc).__name__}: {exc}")
        try:
            return ("ok", json.loads(raw), None)
        except json.JSONDecodeError as exc:
            return ("corrupt", None, f"invalid JSON: {exc}")
    if saw_not_found:  # pragma: no cover — defensive, loop above always returns
        return ("absent", None, None)
    return ("corrupt", None, "unreachable read state")  # pragma: no cover


def _resolve_run_id(lake_root: Path, run_id: str | None) -> str | None:
    """Resolve *run_id*, defaulting to the most-recent known run when ``None``."""
    if run_id is not None:
        return run_id
    listing = list_runs(lake_root, limit=1)
    runs = listing.get("runs") or []
    if not runs:
        return None
    resolved = runs[0].get("run_id")
    return resolved if isinstance(resolved, str) else None


def _awaiting(run_id: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": "awaiting_artifact", "run_id": run_id, "reason": reason}
    payload.update(extra)
    return payload


def _artifact_error(run_id: str | None, reason: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"state": "artifact_error", "run_id": run_id, "reason": reason}
    payload.update(extra)
    return payload


# ─────────────────────────── run listing ───────────────────────────


def list_runs(lake_root: Path, *, limit: int = 50) -> dict[str, Any]:
    """List known run_ids newest-first with a one-line status summary each.

    Powers the Operator page's run picker + run-history panel (O-o
    keep-old-runs). ``is_demo`` is ``True`` when ``run_id`` starts with
    ``"demo-"`` or ``"demo_"`` — a pure UI label, never affecting data
    honesty. A run whose ``exit-status.json`` is corrupt is INCLUDED with
    ``state="artifact_error"`` for that row, never silently dropped; a run
    with no ``exit-status.json`` yet is included with ``state="unknown"``
    (never fabricated as "running").

    HONESTLY O(number of run directories) capped by *limit* — NOT O(1)
    (universal-engineering-standards: never falsely claim O(1)). Call this
    ONLY from the run picker's own refresh, never from the steady
    chain-map/exit-status poll loop (that's :func:`read_run_status`).
    """
    root = sweep_runs_root(lake_root)
    try:
        entries = [p for p in root.iterdir() if p.is_dir()]
    except FileNotFoundError:
        return {
            "state": "awaiting_artifact",
            "runs": [],
            "reason": f"no sweep run directory found at {root}",
        }
    except NotADirectoryError:
        return {
            "state": "awaiting_artifact",
            "runs": [],
            "reason": f"sweep run root is not a directory: {root}",
        }
    except OSError as exc:
        return {
            "state": "artifact_error",
            "runs": [],
            "reason": f"sweep run root unreadable: {type(exc).__name__}: {exc}",
        }

    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    entries.sort(key=_mtime, reverse=True)
    runs: list[dict[str, Any]] = []
    for entry in entries[: max(0, limit)]:
        run_id = entry.name
        state, data, _err = read_json_artifact(entry / "exit-status.json")
        if state == "ok" and isinstance(data, dict):
            row = {
                "run_id": run_id,
                "state": str(data.get("state", "unknown")),
                "wall_started_utc": data.get("wall_started_utc"),
                "wall_ended_utc": data.get("wall_ended_utc"),
                "suspect": bool(data.get("suspect", False)),
                "is_demo": run_id.startswith("demo-") or run_id.startswith("demo_"),
            }
        elif state == "corrupt":
            row = {
                "run_id": run_id,
                "state": "artifact_error",
                "wall_started_utc": None,
                "wall_ended_utc": None,
                "suspect": False,
                "is_demo": run_id.startswith("demo-") or run_id.startswith("demo_"),
            }
        else:  # absent, or "ok" with a non-dict payload
            row = {
                "run_id": run_id,
                "state": "unknown",
                "wall_started_utc": None,
                "wall_ended_utc": None,
                "suspect": False,
                "is_demo": run_id.startswith("demo-") or run_id.startswith("demo_"),
            }
        runs.append(row)
    return {"state": "ok", "runs": runs}


# ─────────────────────────── door vocabulary ───────────────────────────


def door_labels() -> dict[str, tuple[str, str]]:
    """Return the ordered ``{door_key: (display_label, group)}`` vocabulary.

    Covers every door name P2/P3 may ever emit into ``manifest.per_door_kills``:
    the 4 populated-today doors (``min_hits``, ``no_bars``, ``cache_skip``,
    ``resume_skip``), the 6 cascade doors (``dsr``, ``spa``, ``cpcv``,
    ``walk_forward``, ``k_of_n``, ``robustness`` — D-8 contract naming), and
    the 5 hard-gate doors from R19's institutional gate list (``min_trades``,
    ``profit_factor``, ``max_drawdown``, ``win_rate``, ``consecutive_losses``)
    — currently ALWAYS absent per gap-register finding #12 (P3's
    ``gate_reasons()`` has not landed). Pure constant, zero I/O — the one
    shared vocabulary :func:`read_manifest`, its tests, and the funnel table
    in the templates key off, so they can never drift on door names/labels.
    """
    return {
        "min_hits": ("Min-hits gate", "gates"),
        "no_bars": ("No-bars gate", "gates"),
        "cache_skip": ("Cache skip (dedup)", "dedup"),
        "resume_skip": ("Resume skip (checkpoint)", "dedup"),
        "dsr": ("DSR — Deflated Sharpe Ratio", "cascade"),
        "spa": ("SPA — Superior Predictive Ability", "cascade"),
        "cpcv": ("CPCV — Combinatorial Purged CV", "cascade"),
        "walk_forward": ("Walk-forward validation", "cascade"),
        "k_of_n": ("K-of-N robustness", "cascade"),
        "robustness": ("Robustness filter", "cascade"),
        "min_trades": ("Min trades (hard gate, ≥30)", "hard_gate"),
        "profit_factor": ("Profit factor (hard gate, ≥1.2)", "hard_gate"),
        "max_drawdown": ("Max drawdown (hard gate, ≤30%)", "hard_gate"),
        "win_rate": ("Win-rate rule (hard gate)", "hard_gate"),
        "consecutive_losses": ("Consecutive losses (hard gate, ≤10)", "hard_gate"),
    }


# ─────────────────────────── chain map / exit status / manifest ───────────────────────────


def read_chain_map(lake_root: Path, run_id: str) -> dict[str, Any]:
    """O(1) read of ``sweep_runs/<run_id>/chain-map.json`` (R41 live CHAIN MAP)."""
    run_id = validate_run_id(run_id)
    path = sweep_runs_root(lake_root) / run_id / "chain-map.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return _awaiting(
            run_id, f"chain-map.json not found for run {run_id!r} — P2 backend not yet merged"
        )
    if state == "corrupt":
        return _artifact_error(run_id, f"chain-map.json present but unreadable: {err}")
    if not isinstance(data, dict):
        return _artifact_error(run_id, "chain-map.json is not a JSON object")
    return {
        "state": "ok",
        "run_id": data.get("run_id", run_id),
        "links": data.get("links", {}),
        "suspect": bool(data.get("suspect", False)),
        "updated_utc": data.get("updated_utc"),
        "schema_version": data.get("schema_version", 1),
    }


def read_exit_status(lake_root: Path, run_id: str) -> dict[str, Any]:
    """O(1) read of ``sweep_runs/<run_id>/exit-status.json`` (R43/R39/R42)."""
    run_id = validate_run_id(run_id)
    path = sweep_runs_root(lake_root) / run_id / "exit-status.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return _awaiting(
            run_id, f"exit-status.json not found for run {run_id!r} — P2 backend not yet merged"
        )
    if state == "corrupt":
        return _artifact_error(run_id, f"exit-status.json present but unreadable: {err}")
    if not isinstance(data, dict):
        return _artifact_error(run_id, "exit-status.json is not a JSON object")
    return {
        "state": "ok",
        "run_id": data.get("run_id", run_id),
        "run_state": data.get("state", "unknown"),
        "exit_code": data.get("exit_code"),
        "refusal_text": data.get("refusal_text"),
        "error_type": data.get("error_type"),
        "attempt": data.get("attempt"),
        "pid": data.get("pid"),
        "wall_started_utc": data.get("wall_started_utc"),
        "wall_ended_utc": data.get("wall_ended_utc"),
        "suspect": bool(data.get("suspect", False)),
    }


def read_manifest(lake_root: Path, run_id: str) -> dict[str, Any]:
    """O(1) read of ``sweep_runs/<run_id>/manifest.json`` (R28 funnel/kills)."""
    run_id = validate_run_id(run_id)
    path = sweep_runs_root(lake_root) / run_id / "manifest.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return _awaiting(
            run_id, f"manifest.json not found for run {run_id!r} — P2 backend not yet merged"
        )
    if state == "corrupt":
        return _artifact_error(run_id, f"manifest.json present but unreadable: {err}")
    if not isinstance(data, dict):
        return _artifact_error(run_id, "manifest.json is not a JSON object")

    vocabulary = door_labels()
    observed: dict[str, Any] = {}
    raw_kills = data.get("per_door_kills")
    if isinstance(raw_kills, list):
        for entry in raw_kills:
            if isinstance(entry, dict) and isinstance(entry.get("door"), str):
                observed[entry["door"]] = entry.get("killed")

    per_door_kills_by_name: dict[str, dict[str, Any]] = {}
    for door_key in vocabulary:
        if door_key in observed:
            per_door_kills_by_name[door_key] = {"observed": True, "killed": observed[door_key]}
        else:
            per_door_kills_by_name[door_key] = {"observed": False, "killed": None}
    # A door observed on disk but outside the known vocabulary (a future P3
    # door not yet added here) is still surfaced, never silently dropped.
    for door_key, killed in observed.items():
        if door_key not in vocabulary:
            per_door_kills_by_name[door_key] = {"observed": True, "killed": killed}

    result: dict[str, Any] = dict(data)
    result["state"] = "ok"
    result["run_id"] = data.get("run_id", run_id)
    result["per_door_kills_by_name"] = per_door_kills_by_name
    return result


def read_run_status(lake_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Bundle chain-map + exit-status + manifest for one run in ONE call.

    THE single poll target for the whole live-progress area (chain map + id
    strip + funnel table + suspect banner) — 3 tiny atomic JSON reads bundled
    per the minimal-poll bias, not 3 separate polling timers. ``run_id=None``
    resolves to the most recently modified run directory (via
    :func:`list_runs`); the resolved id is echoed back in the response so the
    UI's run picker stays in sync.
    """
    resolved = _resolve_run_id(lake_root, run_id)
    if resolved is None:
        reason = "no sweep run has ever launched"
        return {
            "run_id": None,
            "chain_map": _awaiting(None, reason),
            "exit_status": _awaiting(None, reason),
            "manifest": _awaiting(None, reason),
        }
    resolved = validate_run_id(resolved)
    return {
        "run_id": resolved,
        "chain_map": read_chain_map(lake_root, resolved),
        "exit_status": read_exit_status(lake_root, resolved),
        "manifest": read_manifest(lake_root, resolved),
    }


# ─────────────────────────── preflight / estimator calibration ───────────────────────────


def _coarse_match(match: Mapping[str, Any] | None, selection: Mapping[str, Any]) -> bool:
    """Return ``True`` if *selection* coarsely overlaps every key in *match*.

    Each key in *match* names a selection field (e.g. ``symbols``,
    ``intervals``, ``k``); its value is treated as a candidate set. A missing
    match key is a wildcard (matches anything). A missing selection field
    fails the match key (fixture opted into a criterion the caller didn't
    supply). Overlap, not exact-equality, so a multi-symbol selection still
    matches a single-symbol fixture criterion.
    """
    if not match:
        return True
    for key, wanted in match.items():
        wanted_set = set(wanted) if isinstance(wanted, (list, tuple, set)) else {wanted}
        if key not in selection:
            return False
        got = selection[key]
        got_set = set(got) if isinstance(got, (list, tuple, set)) else {got}
        if not (wanted_set & got_set):
            return False
    return True


def preflight_estimate(lake_root: Path, selection: Mapping[str, Any]) -> dict[str, Any]:
    """Live-ish "this selection = X trials, est. Y, Z GB — proceed?" preview.

    Reads ``tests/fixtures/oneclick/preflight_fixtures/*.json`` (coarse match
    on the fixture's own ``"match"`` block vs *selection*'s ``symbols`` /
    ``intervals`` / ``k`` etc.) when ``$BRUTEX_ONECLICK_FIXTURE_ROOT`` is set.
    In production (P1 not yet merged) always returns
    ``{"state": "awaiting_artifact", ...}`` — never a fabricated number.

    THIN SEAM: at merge-integration this function's body is replaced with a
    direct call into P1's real estimator; callers (the route) never change.
    """
    if not os.environ.get(FIXTURE_ROOT_ENV):
        return {
            "state": "awaiting_artifact",
            "reason": "P1 estimator/preflight backend not yet merged — no live estimate available",
        }
    fixture_dir = sweep_runs_root(lake_root).parent / "preflight_fixtures"
    try:
        candidates = sorted(fixture_dir.glob("*.json"))
    except OSError:
        candidates = []
    if not candidates:
        return {
            "state": "awaiting_artifact",
            "reason": f"no preflight fixtures found under {fixture_dir}",
        }

    fallback: dict[str, Any] | None = None
    chosen: dict[str, Any] | None = None
    for path in candidates:
        state, data, _err = read_json_artifact(path)
        if state != "ok" or not isinstance(data, dict):
            continue
        if path.name == "default.json":
            # default.json is the LAST-RESORT fallback, never a match
            # candidate itself — its typically-empty "match" block would
            # otherwise coarse-match every selection and always win.
            fallback = data
            continue
        if chosen is None and _coarse_match(data.get("match"), selection):
            chosen = data
    picked = chosen or fallback
    if picked is None:
        return {
            "state": "awaiting_artifact",
            "reason": f"preflight fixtures present but none readable under {fixture_dir}",
        }
    result = {k: v for k, v in picked.items() if k != "match"}
    result.setdefault("estimate_basis", "provisional")
    return result


def read_estimator_calibration(lake_root: Path) -> dict[str, Any]:
    """Read ``estimator_calibration.json`` — the PROVISIONAL-vs-calibrated badge source.

    Production path: ``lake_root / "estimator_calibration.json"`` (a sibling
    of ``lake_root / "sweep_runs"``). Fixture path (when
    ``$BRUTEX_ONECLICK_FIXTURE_ROOT`` is set): ``fixture_root.parent /
    "estimator_calibration.json"`` — the sibling of the fixture
    ``sweep_runs`` directory, mirroring the production layout.
    """
    override = os.environ.get(FIXTURE_ROOT_ENV)
    if override:
        path = Path(override).parent / "estimator_calibration.json"
    else:
        path = lake_root / "estimator_calibration.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return _awaiting(None, f"estimator_calibration.json not found at {path}")
    if state == "corrupt":
        return _artifact_error(None, f"estimator_calibration.json present but unreadable: {err}")
    if not isinstance(data, dict):
        return _artifact_error(None, "estimator_calibration.json is not a JSON object")
    return {
        "state": "ok",
        "calibrated": bool(data.get("calibrated", False)),
        "trials_per_second": data.get("trials_per_second"),
        "last_run_id": data.get("last_run_id"),
        "updated_utc": data.get("updated_utc"),
    }


# ─────────────────────────── flight-recorder timeline ───────────────────────────


def iter_timeline(
    lake_root: Path,
    run_id: str | None,
    *,
    after_seq: int = 0,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Tail (or bounded-search) ``flight-recorder.jsonl`` for the R41 RUN TIMELINE.

    Remaps each 8-key AuditWriter line ``{seq, prev_hash, ts_utc, actor,
    action, subject, payload, entry_hash}`` to the documented consumer-facing
    superset ``{ts_utc, run_id, kind, payload}`` (``kind`` = ``action``,
    ``run_id`` = ``subject``).

    The FINAL line is torn-tail tolerant: a :class:`~json.JSONDecodeError` on
    the last line is skipped silently (a partially-flushed final line during
    an in-progress run must never crash the reader). A
    :class:`~json.JSONDecodeError` on any NON-final line is surfaced as one
    loud synthetic ``{"kind": "_unreadable_line", ...}`` record — never
    dropped.

    Cursoring uses the 1-based LINE POSITION within the file as the opaque
    ``after_seq`` cursor value (monotonic and append-only-safe by
    construction, independent of the payload's own ``seq`` field; 1-based so
    the default ``after_seq=0`` legitimately means "everything from the
    start" — cursor ``0`` is never a real record's cursor) — so
    ``next_after_seq`` from one call is always safe to pass back as
    ``after_seq`` on the next to fetch only newer records, including
    synthetic ``_unreadable_line`` records.

    When only ``after_seq`` / ``kind`` are given this is a cheap bounded
    cursor-forward read (the steady poll path, ``bounded_scan=False``). When
    ``q`` is given it ADDITIONALLY does a bounded reverse text scan capped at
    2 MiB / 5000 lines (``bounded_scan=True``) — HONESTLY not O(1); the
    docstring here and the UI caller are both required to invoke ``q`` ONLY
    from an explicit search action, never the auto-refresh timer.

    NEVER verifies the hash chain (that's P2's
    ``brutex.audit_verify.auto_verify`` — out of scope for this read-only
    tailer).
    """
    resolved = _resolve_run_id(lake_root, run_id)
    if resolved is None:
        return {
            "state": "awaiting_artifact",
            "run_id": None,
            "records": [],
            "next_after_seq": None,
            "bounded_scan": False,
            "reason": "no sweep run has ever launched",
        }
    resolved = validate_run_id(resolved)
    path = sweep_runs_root(lake_root) / resolved / "flight-recorder.jsonl"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "state": "awaiting_artifact",
            "run_id": resolved,
            "records": [],
            "next_after_seq": None,
            "bounded_scan": False,
            "reason": f"flight-recorder.jsonl not found for run {resolved!r}",
        }
    except OSError as exc:
        return {
            "state": "artifact_error",
            "run_id": resolved,
            "records": [],
            "next_after_seq": None,
            "bounded_scan": False,
            "reason": f"flight-recorder.jsonl present but unreadable: {type(exc).__name__}: {exc}",
        }

    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    last_idx = len(lines) - 1

    bounded_scan = q is not None
    if bounded_scan and last_idx >= 0:
        window_start = 0
        byte_total = 0
        for line_count, i in enumerate(range(last_idx, -1, -1), start=1):
            byte_total += len(lines[i].encode("utf-8")) + 1
            if line_count >= _TIMELINE_SCAN_MAX_LINES or byte_total >= _TIMELINE_SCAN_MAX_BYTES:
                window_start = i
                break
        scan_range: range = range(window_start, last_idx + 1)
    else:
        scan_range = range(0, last_idx + 1)

    q_lower = q.lower() if q else None
    records: list[dict[str, Any]] = []
    for i in scan_range:
        line = lines[i]
        if not line.strip():
            continue
        # 1-based cursor (NOT the 0-based line index) so the default
        # ``after_seq=0`` legitimately means "everything from the start" —
        # cursor 0 is never a real record's cursor, only the "nothing seen
        # yet" sentinel.
        cursor = i + 1
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i == last_idx:
                continue  # torn-tail tolerance — a partially-flushed final line
            records.append(
                {
                    "ts_utc": None,
                    "run_id": resolved,
                    "kind": "_unreadable_line",
                    "payload": {"line_index": i},
                    "_cursor": cursor,
                }
            )
            continue
        if not isinstance(entry, dict):
            records.append(
                {
                    "ts_utc": None,
                    "run_id": resolved,
                    "kind": "_unreadable_line",
                    "payload": {"line_index": i, "reason": "line is not a JSON object"},
                    "_cursor": cursor,
                }
            )
            continue
        records.append(
            {
                "ts_utc": entry.get("ts_utc"),
                "run_id": entry.get("subject", resolved),
                "kind": entry.get("action"),
                "payload": entry.get("payload"),
                "_cursor": cursor,
            }
        )

    if not bounded_scan:
        records = [r for r in records if r["_cursor"] > after_seq]
    if kind is not None:
        records = [r for r in records if r["kind"] == kind]
    if q_lower is not None:
        records = [
            r
            for r in records
            if q_lower
            in json.dumps({k: v for k, v in r.items() if k != "_cursor"}, default=str).lower()
        ]

    bounded_limit = max(1, int(limit)) if limit else len(records)
    limited = records[:bounded_limit]
    next_after_seq = limited[-1]["_cursor"] if limited else None
    for r in limited:
        r.pop("_cursor", None)

    return {
        "state": "ok",
        "run_id": resolved,
        "records": limited,
        "next_after_seq": next_after_seq,
        "bounded_scan": bounded_scan,
    }


# ─────────────────────────── top-100 leaderboard ───────────────────────────


def _mask_bit_count(mask_hex: Any) -> int:
    """Count set bits in a ``mask_hex`` string; ``0`` for anything unparsable."""
    if not mask_hex:
        return 0
    try:
        return bin(int(str(mask_hex), 16)).count("1")
    except (TypeError, ValueError):
        return 0


def _is_better_family_rep(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """``True`` if *candidate* should replace *current* as the family's representative.

    Fewer set mask bits wins (simplest behavior); ties broken by lower
    ``rank`` (better-ranked wins).
    """
    cand_bits = _mask_bit_count(candidate.get("mask_hex"))
    cur_bits = _mask_bit_count(current.get("mask_hex"))
    if cand_bits != cur_bits:
        return cand_bits < cur_bits
    cand_rank = candidate.get("rank")
    cur_rank = current.get("rank")
    cand_rank = cand_rank if isinstance(cand_rank, (int, float)) else float("inf")
    cur_rank = cur_rank if isinstance(cur_rank, (int, float)) else float("inf")
    return cand_rank < cur_rank


def dedupe_family_representatives(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse each ``family_id`` group to its simplest (fewest-bit) member.

    Rows lacking ``family_id`` pass through untouched. Within a family, the
    member with the fewest set mask bits wins (ties broken by lower
    ``rank``). R34 defensive safety net — guarantees the top-100 the operator
    SEES reads as genuinely different behaviors even if ``top100.json``
    itself hasn't deduped families yet. Pure, O(n) single pass + O(1)
    per-family lookups (no I/O), independently unit-testable. Output
    preserves each surviving row's original first-seen position.
    """
    output: list[dict[str, Any]] = []
    family_slot: dict[Any, int] = {}
    for row in rows:
        family_id = row.get("family_id")
        if family_id is None:
            output.append(row)
            continue
        slot = family_slot.get(family_id)
        if slot is None:
            family_slot[family_id] = len(output)
            output.append(row)
        elif _is_better_family_rep(row, output[slot]):
            output[slot] = row
    return output


def _family_members_map(raw_rows: list[dict[str, Any]]) -> dict[Any, list[dict[str, Any]]]:
    """Group the PRE-dedupe raw top100 rows by ``family_id`` for the R34 drill-down.

    Returns ``{family_id: [raw_row, ...]}`` for every ``family_id`` that
    appears on 2+ rows — a "family" of exactly one row has no twin to drill
    into, so it is omitted. Rows lacking ``family_id`` are never grouped.
    Pure, O(n) single pass over the SAME row objects
    :func:`dedupe_family_representatives` consumes (identity-preserving —
    the representative row is one of the objects in its own group's list, so
    :func:`read_top100` can pick it out with an identity check, never a
    fragile value comparison).
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in raw_rows:
        family_id = row.get("family_id")
        if family_id is None:
            continue
        groups.setdefault(family_id, []).append(row)
    return {family_id: members for family_id, members in groups.items() if len(members) > 1}


def _decorate_top100_row(raw: Mapping[str, Any], run_id: str) -> dict[str, Any]:
    """Decorate one raw top100.json row with join-key back-fill + decode_combo().

    Join-key law (sweep-parallel-integration-contracts.md §3, "Ruling 17
    status" — the correction over the earlier gap-register #17 sketch this
    function was originally written against): the real per-combo join key
    the P3 SHIPPED ``top100.json`` entries carry is the entry-level
    ``run_id`` field — "the per-combo blake3 store key ... serving as the
    combo join id into ResultsStore + trade_spans". ``mode`` is explicitly
    NOT a per-entry key in the shipped shape, and the run-LEVEL id was never
    renamed ``sweep_run_id`` (the object root's key is plain ``run_id``,
    passed in here as *run_id* — always genuinely known, since it is how the
    caller located this file, so it is never treated as inferred).
    """
    join_key_inferred = False

    instrument = raw.get("instrument")
    if not instrument:
        instrument = "—"
        join_key_inferred = True

    mask_hex = raw.get("mask_hex")
    if not mask_hex:
        mask_hex = "0"
        join_key_inferred = True
    direction = raw.get("direction")
    if not direction:
        # Honest-absence discipline (module docstring): "long" is a
        # concrete, potentially opposite-of-real trading direction, so a
        # fabricated default here MUST flip the same inferred signal the
        # instrument/combo_id back-fill uses below — never render a guess
        # as though it were the real value.
        direction = "long"
        join_key_inferred = True
    tf = raw.get("tf")
    if not tf:
        tf = ""
        join_key_inferred = True

    # The real per-combo join key (see docstring): the shipped entry's own
    # `run_id`, surfaced under the `combo_id` key P4's templates/JS already
    # consume (results.html / v29.html / strategy.html) — only synthesize
    # the legacy placeholder when the real key is genuinely absent (a row
    # written before P3 shipped, or a hand-built legacy fixture).
    combo_id = raw.get("run_id")
    if combo_id is None:
        combo_id = f"legacy-{mask_hex}-{direction}-{tf}"
        join_key_inferred = True
    # The run-LEVEL id is how the caller resolved this top100.json in the
    # first place (see read_top100) — always known, never inferred.
    sweep_run_id = run_id

    try:
        mask_int = int(str(mask_hex), 16)
    except (TypeError, ValueError):
        mask_int = 0
    explanation = decode_combo(mask_int, direction, tf)

    return {
        "rank": raw.get("rank"),
        "mask_hex": mask_hex,
        "conditions": raw.get("conditions", []),
        "tf": tf,
        "direction": direction,
        "arm": raw.get("arm"),
        "metrics": raw.get("metrics", {}),
        "regime": raw.get("regime", {}),
        "vix_band": raw.get("vix_band", {}),
        "family_id": raw.get("family_id"),
        "instrument": instrument,
        "combo_id": combo_id,
        "sweep_run_id": sweep_run_id,
        "join_key_inferred": join_key_inferred,
        "simple_sentence": explanation.simple_sentence,
        "pro_sentence": explanation.pro_sentence,
        "regime_label": explanation.regime_label,
    }


def read_top100(lake_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read + family-dedupe + fully decode ``sweep_runs/<run_id>/top100.json``.

    Backs the Operator page's global cross-bucket Best-of-Best Top-100
    leaderboard (R21/R26). Rows pass through
    :func:`dedupe_family_representatives` first, then each surviving row is
    decorated by calling :func:`~brutex.dashboard.combo_decoder.decode_combo`
    (reused verbatim) plus honest join-key back-fill for legacy rows lacking
    ``instrument`` or the real per-combo join key (the shipped entry-level
    ``run_id`` field, surfaced as ``combo_id`` — sweep-parallel-integration-
    contracts.md §3, "Ruling 17 status") — such rows carry
    ``join_key_inferred=True``.

    The on-disk root accepts EITHER a bare JSON array of rows (legacy/fixture
    shape) OR the P3 SHIPPED object root (sweep-parallel-integration-
    contracts.md §3, DEC-SWEEP-RESULTS-DEPTH-001 / PR #1275) —
    ``{schema_version, run_id, preset, source{...}, baselines{...}, entries:
    [...]}`` — in which case ``entries`` is unwrapped transparently before
    dedupe/decoration.
    """
    resolved = _resolve_run_id(lake_root, run_id)
    if resolved is None:
        return {
            "state": "awaiting_artifact",
            "run_id": None,
            "rows": [],
            "reason": "no sweep run has ever launched",
        }
    resolved = validate_run_id(resolved)
    path = sweep_runs_root(lake_root) / resolved / "top100.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return _awaiting(
            resolved,
            f"top100.json not found for run {resolved!r} — P3 backend not yet merged",
            rows=[],
        )
    if state == "corrupt":
        return _artifact_error(resolved, f"top100.json present but unreadable: {err}", rows=[])
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict) and isinstance(data.get("entries"), list):
        # P3 SHIPPED shape (sweep-parallel-integration-contracts.md §3,
        # DEC-SWEEP-RESULTS-DEPTH-001 / PR #1275): the on-disk root is an
        # OBJECT wrapping {schema_version, run_id, preset, source{...},
        # baselines{...}, entries: [...]}, not a bare list — unwrap it here.
        entries = data["entries"]
    else:
        return _artifact_error(
            resolved,
            "top100.json is not a JSON array or an object with an 'entries' list",
            rows=[],
        )

    raw_dicts = [r for r in entries if isinstance(r, dict)]
    deduped = dedupe_family_representatives(raw_dicts)
    family_groups = _family_members_map(raw_dicts)
    rows: list[dict[str, Any]] = []
    for raw_row in deduped:
        decorated = _decorate_top100_row(raw_row, resolved)
        members = family_groups.get(raw_row.get("family_id"), [])
        # ``members`` includes the representative itself (identity, not value,
        # comparison — two distinct rows can be byte-identical dicts) — the
        # R34 twin list on drill-down is every OTHER member of the family.
        twins = [m for m in members if m is not raw_row]
        decorated["family_size"] = len(members) if members else 1
        decorated["family_members"] = [
            {
                "rank": twin.get("rank"),
                "mask_hex": twin.get("mask_hex"),
                "combo_id": twin.get("combo_id"),
                "tf": twin.get("tf"),
                "direction": twin.get("direction"),
                "conditions": twin.get("conditions", []),
            }
            for twin in twins
        ]
        rows.append(decorated)
    return {"state": "ok", "run_id": resolved, "rows": rows}


# ─────────────────────────── trade list ───────────────────────────


def read_trades_page(
    lake_root: Path,
    run_id: str | None,
    rank: int,
    *,
    mask_hex: str | None = None,
    direction: str | None = None,
    tf: str | None = None,
    offset: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    """Read one page of a combo's trade list — R24 every-trade-visible audit trail.

    Only resolves real rows when running under
    ``$BRUTEX_ONECLICK_FIXTURE_ROOT`` (the ``trades/rank-%04d.json`` layout is
    fixture-ONLY per the contracts file); against a production root it
    honestly returns ``state="awaiting_artifact"`` rather than guessing a
    path P3 has not shipped. ``lots``/``costs_paisa`` pass through as-is
    (``None`` stays ``None`` — gap-register finding #18, NULL-by-design until
    P5). Every row also passes through the three P3 SHIPPED additive
    ``TradeRowWire`` keys verbatim — ``trade_seq``, ``path_1m_status``,
    ``mae_granularity`` (``"1m"``/``"tf"`` — sweep-parallel-integration-
    contracts.md §1 — says which granularity ``mae``/``mfe`` actually carry
    so a consumer never guesses); this function does not need to interpret
    them itself, but MUST NOT drop them — :mod:`strategy.html`/
    :mod:`results.html` read ``row.mae_granularity``/``row.path_1m_status``
    to mark a TF-level (R22 non-compliant) MAE/MFE value in the UI instead of
    rendering it identically to a verified 1-minute value.
    ``combo_explanation`` is populated by calling
    :func:`~brutex.dashboard.combo_decoder.decode_combo` once when
    *mask_hex*/*direction*/*tf* are all supplied. *limit* is capped
    server-side at 500.
    """
    resolved = _resolve_run_id(lake_root, run_id)
    if resolved is None:
        return {
            "state": "awaiting_artifact",
            "run_id": None,
            "rank": rank,
            "rows": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "combo_explanation": None,
            "reason": "no sweep run has ever launched",
        }
    resolved = validate_run_id(resolved)
    limit = max(1, min(int(limit), _TRADES_LIMIT_MAX))
    offset = max(0, int(offset))

    combo_explanation: dict[str, Any] | None = None
    if mask_hex is not None and direction is not None and tf is not None:
        try:
            mask_int = int(str(mask_hex), 16)
        except (TypeError, ValueError):
            mask_int = 0
        explanation = decode_combo(mask_int, direction, tf)
        combo_explanation = {
            "conditions": [
                {
                    "bit": c.bit,
                    "technical_name": c.technical_name,
                    "pro_label": c.pro_label,
                    "simple_label": c.simple_label,
                }
                for c in explanation.conditions
            ],
            "simple_sentence": explanation.simple_sentence,
            "pro_sentence": explanation.pro_sentence,
            "regime_label": explanation.regime_label,
            "direction_label": explanation.direction_label,
        }

    if not os.environ.get(FIXTURE_ROOT_ENV):
        return {
            "state": "awaiting_artifact",
            "run_id": resolved,
            "rank": rank,
            "rows": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "combo_explanation": combo_explanation,
            "reason": "trade list is fixture-only — production ResultsStore paged API not yet merged (P3)",
        }

    path = sweep_runs_root(lake_root) / resolved / "trades" / f"rank-{int(rank):04d}.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return {
            "state": "awaiting_artifact",
            "run_id": resolved,
            "rank": rank,
            "rows": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "combo_explanation": combo_explanation,
            "reason": f"no trade list for rank {rank} on run {resolved!r}",
        }
    if state == "corrupt":
        return {
            "state": "artifact_error",
            "run_id": resolved,
            "rank": rank,
            "rows": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "combo_explanation": combo_explanation,
            "reason": f"trades file present but unreadable: {err}",
        }
    if not isinstance(data, dict):
        return {
            "state": "artifact_error",
            "run_id": resolved,
            "rank": rank,
            "rows": [],
            "total": 0,
            "offset": offset,
            "limit": limit,
            "combo_explanation": combo_explanation,
            "reason": "trades file is not a JSON object",
        }

    all_rows = data.get("rows", [])
    if not isinstance(all_rows, list):
        all_rows = []
    total = data.get("total", len(all_rows))
    if not isinstance(total, int):
        total = len(all_rows)
    page = all_rows[offset : offset + limit]

    return {
        "state": "ok",
        "run_id": resolved,
        "rank": rank,
        "rows": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "combo_explanation": combo_explanation,
    }


# ─────────────────────────── VERIFY-ME ───────────────────────────


def _awaiting_verify_checks(detail: str) -> dict[str, dict[str, str]]:
    return {check: {"status": "awaiting", "detail": detail} for check in VERIFY_ME_CHECKS}


def read_verify_me(lake_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Read the 7-key R44 operator-acceptance verdict — render-only (Ruling 11).

    A totally absent ``verify-me.json`` SYNTHESIZES the honest default (all 7
    checks ``"awaiting"``, ``detail="P2 audit-verify backend not yet
    merged"``) rather than requiring every fixture to carry the file. A
    present-but-corrupt file returns ``state="artifact_error"`` with all 7
    checks forced back to ``"awaiting"`` plus a loud top-level unreadable
    message. NEVER computes or triggers a check itself.
    """
    resolved = _resolve_run_id(lake_root, run_id)
    if resolved is None:
        return {
            "state": "ok",
            "run_id": None,
            "is_demo": False,
            "checks": _awaiting_verify_checks("no sweep run has ever launched"),
        }
    resolved = validate_run_id(resolved)
    is_demo = resolved.startswith("demo-") or resolved.startswith("demo_")
    path = sweep_runs_root(lake_root) / resolved / "verify-me.json"
    state, data, err = read_json_artifact(path)
    if state == "absent":
        return {
            "state": "ok",
            "run_id": resolved,
            "is_demo": is_demo,
            "checks": _awaiting_verify_checks("P2 audit-verify backend not yet merged"),
        }
    if state == "corrupt":
        return {
            "state": "artifact_error",
            "run_id": resolved,
            "is_demo": is_demo,
            "reason": f"verify-me.json present but unreadable: {err}",
            "checks": _awaiting_verify_checks("verify-me.json present but unreadable"),
        }
    if not isinstance(data, dict):
        return {
            "state": "artifact_error",
            "run_id": resolved,
            "is_demo": is_demo,
            "reason": "verify-me.json is not a JSON object",
            "checks": _awaiting_verify_checks("verify-me.json is not a JSON object"),
        }

    checks_raw = data.get("checks")
    checks: dict[str, dict[str, str]] = {}
    for name in VERIFY_ME_CHECKS:
        entry = checks_raw.get(name) if isinstance(checks_raw, dict) else None
        if isinstance(entry, dict) and entry.get("status") in _VERIFY_ME_STATUSES:
            checks[name] = {"status": str(entry["status"]), "detail": str(entry.get("detail", ""))}
        else:
            checks[name] = {
                "status": "awaiting",
                "detail": "P2 audit-verify backend not yet merged",
            }

    return {
        "state": "ok",
        "run_id": resolved,
        "is_demo": bool(data.get("is_demo", is_demo)),
        "checks": checks,
    }


# ─────────────────────────── presentation-only status badges ───────────────────────────
#
# Pure presentation mapping (DEC-INSTITUTIONAL-ONECLICK-001), living in this
# module rather than a second free-standing one because the P4 ownership
# grant is a single adapter file, not a second module (round-3 review
# finding). Mirrors :func:`~brutex.dashboard.verdict_banner.banner_for`'s
# pattern: maps an *already-computed* status/state string onto a small,
# deterministic presentation dict — a CSS modifier class + a human colour
# name + a display label — that a caller renders verbatim. Pure O(1) dict
# lookups, zero I/O.
#
# Two vocabularies are covered:
#
# - :func:`check_badge_for` — the R44 VERIFY-ME per-check status
#   (``pass``/``fail``/``awaiting``, see :data:`VERIFY_ME_CHECKS` /
#   :func:`read_verify_me`).
# - :func:`link_badge_for` — the R41 chain-map link state
#   (``pending``/``running``/``green``/``amber``/``red``, see
#   :func:`read_chain_map`).
#
# **No server-side templating exists in this dashboard** (see module
# docstring above: plain static HTML + client-side JS, never Jinja) — so
# these functions are not literally `import`-ed by a template. They are
# instead the documented reference mapping that a client-side mirror is
# expected to stay consistent with: ``templates/verify_me.html``'s inline
# ``STATUS_STYLE`` map carries an explicit comment pointing back here.
# ``templates/v29.html``'s pre-existing inline ``CM_STATE_CLASS`` map has no
# such back-reference comment today, and no test reads either template's JS
# map to pin it against :func:`link_badge_for` / :func:`check_badge_for` —
# that regression-test gap is open, not asserted-away here.


@dataclass(frozen=True, slots=True)
class StatusBadge:
    """The display-ready badge for one status/state value.

    Immutable + JSON-serialisable. Every field is either a pure presentation
    decision (``css_class``/``colour``/``label``) or a verbatim echo of the
    input ``status`` — never a re-derivation.
    """

    status: str
    css_class: str
    colour: str
    label: str

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-friendly dict (e.g. for a future JSON-serving route)."""
        return {
            "status": self.status,
            "css_class": self.css_class,
            "colour": self.colour,
            "label": self.label,
        }


#: R44 VERIFY-ME per-check status → badge. Reuses the same green/red/amber
#: severity vocabulary as :mod:`brutex.dashboard.verdict_banner` (green =
#: trustworthy, red = broken, amber = not yet knowable — never a blank or
#: silently-omitted state).
_CHECK_STYLE: Final[dict[str, tuple[str, str, str]]] = {
    "pass": ("bx-chip ok", "green", "PASS"),
    "fail": ("bx-chip bad", "red", "FAIL"),
    "awaiting": ("bx-chip warn", "amber", "AWAITING"),
}

#: R41 chain-map link state → badge. Intended to be byte-consistent with
#: ``templates/v29.html``'s pre-existing inline ``CM_STATE_CLASS`` map
#: (``{pending: 'muted', running: 'info', green: 'ok', amber: 'warn', red:
#: 'bad'}``) — this module documents that mapping's canonical source, though
#: (see section note above) nothing currently tests the two stay in sync.
_LINK_STYLE: Final[dict[str, tuple[str, str, str]]] = {
    "pending": ("bx-chip muted", "grey", "pending"),
    "running": ("bx-chip info", "blue", "running"),
    "green": ("bx-chip ok", "green", "green"),
    "amber": ("bx-chip warn", "amber", "amber"),
    "red": ("bx-chip bad", "red", "red"),
}


def check_badge_for(status: str) -> StatusBadge:
    """Map an R44 VERIFY-ME check ``status`` onto its display-ready badge.

    Parameters
    ----------
    status
        One of ``"pass"``/``"fail"``/``"awaiting"`` (the
        :data:`_VERIFY_ME_STATUSES` vocabulary).

    Raises
    ------
    ValueError
        If *status* is not one of the three known values — a fail-closed
        guard so an unknown status is never rendered uncoloured / silently
        treated as a pass.
    """
    style = _CHECK_STYLE.get(status)
    if style is None:
        raise ValueError(
            f"unknown VERIFY-ME check status {status!r} — expected one of {sorted(_CHECK_STYLE)}"
        )
    css_class, colour, label = style
    return StatusBadge(status=status, css_class=css_class, colour=colour, label=label)


def link_badge_for(state: str) -> StatusBadge:
    """Map an R41 chain-map link ``state`` onto its display-ready badge.

    Parameters
    ----------
    state
        One of ``"pending"``/``"running"``/``"green"``/``"amber"``/``"red"``
        (the chain-map link state vocabulary from :func:`read_chain_map`).

    Raises
    ------
    ValueError
        If *state* is not one of the five known values.
    """
    style = _LINK_STYLE.get(state)
    if style is None:
        raise ValueError(
            f"unknown chain-map link state {state!r} — expected one of {sorted(_LINK_STYLE)}"
        )
    css_class, colour, label = style
    return StatusBadge(status=state, css_class=css_class, colour=colour, label=label)


__all__ = [
    "FIXTURE_ROOT_ENV",
    "VERIFY_ME_CHECKS",
    "ReadState",
    "StatusBadge",
    "check_badge_for",
    "dedupe_family_representatives",
    "door_labels",
    "iter_timeline",
    "link_badge_for",
    "list_runs",
    "preflight_estimate",
    "read_chain_map",
    "read_estimator_calibration",
    "read_exit_status",
    "read_json_artifact",
    "read_manifest",
    "read_run_status",
    "read_top100",
    "read_trades_page",
    "read_verify_me",
    "sweep_runs_root",
    "validate_run_id",
]
