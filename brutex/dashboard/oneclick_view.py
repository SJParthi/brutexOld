"""One-click operator-page PAYLOAD builders — pure, no I/O, no FastAPI (DEC-OPERATOR-PAGE-REAL-001).

Every function here maps the pre-narrowed :mod:`brutex.dashboard.oneclick_artifacts`
result types into the ``/api/v1/oneclick/*`` JSON payload schemas. Pure by
design (CLAUDE.md §11 COMMON): the adapter does the disk I/O + isinstance
narrowing ONCE; these builders are deterministic dict projections the unit
suite drives to 100% standalone.

Honesty laws bound here (R27 / Golden Rule #1):

* NO invented text — every "state" word is DERIVED mechanically and the
  payload NAMES the derivation (``derived_from``).
* Counts are never fabricated — ``row_count`` is always ``len(rows)`` (never
  an assumed 100), a missing total stays ``None``, missing funnel keys make
  the reconcile verdict ``ok=None`` (awaiting), never a fake pass/fail.
* Metrics pass through UNTOUCHED at full precision (money 2dp is client-side
  via ``bx-format.js``'s ``bxFixed2`` ONLY — DEC-MONEY-2DP-001).
* The R44 VERIFY-ME panel uses a 5-state truth table
  (``pass | fail | ready | awaiting | not_wired``): operator-judged checks
  (#3/#4) can NEVER exceed ``ready`` server-side, and un-wired capabilities
  (#1/#5/#6) can NEVER read ``pass`` — a green badge is EARNED, not promised.
* Every builder stamps ``fixture_root_active`` into its payload (C-MINOR-1)
  so fixture-backed data is always visibly labeled.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Final

from brutex.dashboard.oneclick_artifacts import (
    BaselineResult,
    EventsPage,
    ManifestResult,
    RunInfo,
    Top100Result,
    TradesResult,
)

__all__ = [
    "CHAIN_STAGES",
    "build_baseline_payload",
    "build_equity_payload",
    "build_events_payload",
    "build_manifest_payload",
    "build_runs_payload",
    "build_top100_payload",
    "build_trades_payload",
    "build_verify_payload",
    "cascade_status_text",
    "chain_links",
    "reconcile_funnel",
    "run_pill_state",
    "verify_checks",
]

#: The R41 chain-map stage order (fixed — preflight→…→display).
CHAIN_STAGES: Final[tuple[str, ...]] = (
    "preflight",
    "sweep",
    "gates",
    "rank",
    "refine",
    "cascade",
    "artifacts",
    "display",
)

#: The permanent chain-check honesty caveat (T-M1) — must appear on EVERY
#: chain verdict detail, cached or fresh.
_CHAIN_ASSUMED: Final[str] = "(algorithm assumed until P2 merges)"


def _ns_to_utc_iso(mtime_ns: int) -> str:
    """Render a stat ``st_mtime_ns`` as a second-precision UTC ISO string."""
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_runs_payload(runs: Sequence[RunInfo], *, fixture_root_active: bool) -> dict[str, Any]:
    """Project discovered runs into the ``GET /api/v1/oneclick/runs`` payload.

    Parameters
    ----------
    runs
        Newest-first :class:`~brutex.dashboard.oneclick_artifacts.RunInfo`
        rows from ``discover_runs`` (empty ⇒ the honest awaiting state).
    fixture_root_active
        Whether the fixture-root override is serving this payload (stamped —
        C-MINOR-1).

    Returns
    -------
    dict
        ``{status, runs[], latest_run_id, fixture_root_active, detail}``.
    """
    rows = [
        {
            "run_id": info.run_id,
            "mtime_utc": _ns_to_utc_iso(info.dir_mtime_ns),
            "has_manifest": info.has_manifest,
            "has_top100": info.has_top100,
            "has_events": info.has_events,
            "suspect": info.suspect,
        }
        for info in runs
    ]
    return {
        "status": "ok" if rows else "awaiting",
        "runs": rows,
        "latest_run_id": rows[0]["run_id"] if rows else None,
        "fixture_root_active": fixture_root_active,
        "detail": (
            "" if rows else "awaiting real run artifact — no runs under <results_root>/runs"
        ),
    }


def run_pill_state(info: RunInfo | None, manifest: ManifestResult | None) -> dict[str, Any]:
    """Derive the run pill's state word MECHANICALLY, naming the derivation (C-MINOR-2).

    Derivation order (first match wins, never invented):

    1. ``suspect`` — the readable manifest carries ``suspect: true``.
    2. ``complete`` — the manifest is readable with ``actual.funnel`` present.
    3. ``artifacts partial`` — the manifest is corrupt, or some artifact
       files are missing.
    4. ``awaiting`` — the run directory itself is absent.
    5. ``pending`` — everything present but no funnel recorded yet.

    Parameters
    ----------
    info
        The run directory probe (``None`` when the directory is absent).
    manifest
        The manifest read verdict (``None`` when not attempted).

    Returns
    -------
    dict
        ``{"state": ..., "derived_from": ...}``.
    """
    if manifest is not None and manifest.manifest is not None:
        if manifest.manifest.get("suspect") is True:
            return {"state": "suspect", "derived_from": "manifest.suspect is true"}
        actual = manifest.manifest.get("actual")
        if isinstance(actual, dict) and "funnel" in actual:
            return {
                "state": "complete",
                "derived_from": "manifest readable with actual.funnel present",
            }
    if manifest is not None and manifest.status == "corrupt":
        return {"state": "artifacts partial", "derived_from": "manifest.json corrupt"}
    if info is None:
        return {
            "state": "awaiting",
            "derived_from": "run directory absent — awaiting real run artifact",
        }
    missing = [
        name
        for present, name in (
            (info.has_manifest, "manifest.json"),
            (info.has_top100, "top100.json"),
            (info.has_events, "events.jsonl"),
        )
        if not present
    ]
    if missing:
        return {"state": "artifacts partial", "derived_from": "missing: " + ", ".join(missing)}
    return {
        "state": "pending",
        "derived_from": "all artifact files present; manifest carries no actual.funnel yet",
    }


def build_manifest_payload(
    run_id: str,
    manifest: ManifestResult,
    *,
    fixture_root_active: bool,
    info: RunInfo | None = None,
) -> dict[str, Any]:
    """Project one manifest read into the ``GET /api/v1/oneclick/manifest`` payload.

    Parameters
    ----------
    run_id
        The requested run id (echoed).
    manifest
        The adapter's read verdict — the contract keys pass through verbatim.
    fixture_root_active
        Stamped into the payload (C-MINOR-1).
    info
        Optional run-dir probe feeding the pill derivation (missing-file
        naming); ``None`` degrades the pill honestly.

    Returns
    -------
    dict
        ``{status, run_id, manifest, pill, detail, revision, fixture_root_active}``.
    """
    return {
        "status": manifest.status,
        "run_id": run_id,
        "manifest": manifest.manifest,
        "pill": run_pill_state(info, manifest),
        "detail": manifest.detail,
        "revision": manifest.revision,
        "fixture_root_active": fixture_root_active,
    }


#: The two ``not_validated`` reasons whose display suffix is ACTIONABLE for
#: an operator (a stale report / ambiguous evidence) — every other reason
#: (``no_report`` / ``not_in_cascade_pool``) reads the plain "not validated"
#: (DEC-RESULTS-CASCADE-VERDICT-JOIN-001).
_CASCADE_REASON_SUFFIX: Final[dict[str, str]] = {
    "sweep_id_mismatch": " · sweep_id_mismatch",
    "duplicate_candidate_id": " · duplicate verdict",
}


def cascade_status_text(
    status: str | None,
    *,
    rejected_at: str | None = None,
    provisional: bool = False,
    reason: str | None = None,
) -> str:
    """The ONE server-built cascade-verdict display string (no prose in JS).

    DEC-RESULTS-CASCADE-VERDICT-JOIN-001: every surface (the one-click bridge
    ``oneclick_artifacts._bridge_cascade`` AND the ``/results`` row
    projection in ``sweep_results_view``) renders THIS builder's output via
    ``textContent`` — no client ever composes a "PASS" default. ``None`` /
    absent / an unknown token → the honest "—"; a fail names its VERBATIM
    stage token (unknown FUTURE tokens included — never a guess, ``?`` when
    the report carried none); a provisional pass can never read as a bare
    PASS (the pool-N DSR honesty stamp).
    """
    if status == "provisional_pass":
        return "PASS (provisional)"
    if status == "pass":
        # Defense-in-depth: a "pass" carrying provisional=True (a shape the
        # writer never emits — it mints provisional_pass) still can never
        # read as a bare proven PASS.
        return "PASS (provisional)" if provisional else "PASS"
    if status == "fail":
        return f"FAIL @ {rejected_at or '?'}"
    if status == "not_validated":
        return "not validated" + _CASCADE_REASON_SUFFIX.get(reason or "", "")
    return "—"


def build_top100_payload(
    run_id: str, top100: Top100Result, *, fixture_root_active: bool
) -> dict[str, Any]:
    """Project one top-100 read into the ``GET /api/v1/oneclick/top100`` payload.

    Metrics are NEVER re-rounded server-side (JSON passthrough; money 2dp is
    client-side via ``bxFixed2`` ONLY). ``row_count`` is ALWAYS ``len(rows)``
    — never assumed 100 (T-M2).

    Parameters
    ----------
    run_id
        The requested run id (echoed).
    top100
        The adapter's read verdict.
    fixture_root_active
        Stamped into the payload (C-MINOR-1).

    Returns
    -------
    dict
        ``{status, run_id, rows[], row_count, preset, empty_note, detail,
        revision, fixture_root_active, cascade_join}`` — ``preset`` is the
        artifact's R30 ranking token (the client's sort-basis label source;
        ``null`` until the object root parsed — the label then falls back
        honestly); ``cascade_join`` is the artifact root's ADDITIVE
        Stage-4 join meta (DEC-RESULTS-CASCADE-VERDICT-JOIN-001 — ``null``
        on a pre-change artifact, the clients render "—").
    """
    empty_note = (
        "0 winners stored for this run" if top100.status == "ok" and not top100.rows else None
    )
    return {
        "status": top100.status,
        "run_id": run_id,
        "rows": list(top100.rows),
        "row_count": len(top100.rows),
        "preset": top100.preset,
        "empty_note": empty_note,
        "detail": top100.detail,
        "revision": top100.revision,
        "fixture_root_active": fixture_root_active,
        "cascade_join": top100.cascade_join,
    }


def build_baseline_payload(
    run_id: str, result: BaselineResult, *, fixture_root_active: bool
) -> dict[str, Any]:
    """Project one baseline read into the ``GET /api/v1/oneclick/baseline`` payload.

    STORED values only (DEC-RESULTS-TRUST-HONESTY-001 · B1 — the R27 series is
    served verbatim, never recomputed): prices stay the artifact's exact 2-dp
    strings, ``total_return_pct`` stays the raw full-precision float (a
    statistical figure — never rounded, CLAUDE.md §4). A non-``ok`` read keeps
    the series keys ``null`` with the honest detail + the run's stored
    ``instruments`` so the client can re-ask precisely.

    Parameters
    ----------
    run_id
        The requested run id (echoed).
    result
        The adapter's :class:`~brutex.dashboard.oneclick_artifacts.BaselineResult`.
    fixture_root_active
        Stamped into the payload (C-MINOR-1).

    Returns
    -------
    dict
        ``{status, run_id, instrument, filename, instruments[], anchor_open,
        start, end, sessions[], total_return_pct, source_note, detail,
        revision, fixture_root_active}``.
    """
    series = result.series
    payload: dict[str, Any] = {
        "status": result.status,
        "run_id": run_id,
        "instrument": result.instrument,
        "filename": result.filename,
        "instruments": list(result.instruments),
        "anchor_open": None,
        "start": None,
        "end": None,
        "sessions": None,
        "total_return_pct": None,
        "source_note": None,
        "detail": result.detail,
        "revision": result.revision,
        "fixture_root_active": fixture_root_active,
    }
    if series is not None:
        payload["anchor_open"] = str(series.anchor_open)
        payload["start"] = series.start.isoformat()
        payload["end"] = series.end.isoformat()
        payload["sessions"] = [[day.isoformat(), str(close)] for day, close in series.sessions]
        payload["total_return_pct"] = series.total_return_pct
        payload["source_note"] = series.source_note
    return payload


def build_events_payload(
    run_id: str,
    page: EventsPage,
    *,
    fixture_root_active: bool,
    window_filtered: bool = False,
) -> dict[str, Any]:
    """Project one events tail page into the ``GET /api/v1/oneclick/events`` payload.

    Parameters
    ----------
    run_id
        The requested run id (echoed).
    page
        The adapter's O(page) tail read.
    fixture_root_active
        Stamped into the payload (C-MINOR-1).
    window_filtered
        ``True`` when a ``kind``/``q`` filter was applied — the filter scopes
        the READ WINDOW only (honest: not a whole-file search).

    Returns
    -------
    dict
        ``{status, run_id, records[], next_offset, total_bytes,
        truncated_tail, window_filtered, detail, fixture_root_active}``.
    """
    return {
        "status": page.status,
        "run_id": run_id,
        "records": list(page.records),
        "next_offset": page.next_offset,
        "total_bytes": page.total_bytes,
        "truncated_tail": page.truncated_tail,
        "window_filtered": window_filtered,
        "detail": page.detail,
        "fixture_root_active": fixture_root_active,
    }


def build_trades_payload(
    run_id: str,
    rank: int,
    page: TradesResult,
    *,
    offset: int,
    limit: int,
    fixture_root_active: bool,
) -> dict[str, Any]:
    """Project one trades page into the ``GET /api/v1/oneclick/trades`` payload.

    ``total`` stays ``None`` when unknown — never a fabricated 0; ``source``
    is rendered as a VISIBLE badge client-side (C-M5), never payload-only.
    A ``vix_entry``/``vix_exit`` null passes through as null (R31 honest NULL).

    Parameters
    ----------
    run_id, rank
        The requested coordinates (echoed).
    page
        The reader's page verdict.
    offset, limit
        The requested page window (echoed).
    fixture_root_active
        Stamped into the payload (C-MINOR-1).

    Returns
    -------
    dict
        ``{status, run_id, rank, rows[], total, offset, limit, source,
        detail, fixture_root_active}``.
    """
    return {
        "status": page.status,
        "run_id": run_id,
        "rank": rank,
        "rows": list(page.rows),
        "total": page.total,
        "offset": offset,
        "limit": limit,
        "source": page.source,
        "detail": page.detail,
        "fixture_root_active": fixture_root_active,
    }


def build_equity_payload(
    run_id: str,
    rank: int,
    page: TradesResult,
    *,
    offset: int,
    limit: int,
    fixture_root_active: bool,
) -> dict[str, Any]:
    """Project one equity-series page into the ``GET /api/v1/oneclick/equity`` payload.

    DEC-ONECLICK-WINNER-DETAIL-TRUTH-001 (audit item 4C): the SAME page wire
    shape + honesty laws as the trades payload (``total`` stays ``None`` when
    unknown — never a fabricated 0; ``source`` is a visible badge), over rows
    that are the stored ``{session_date, equity_close_paisa,
    equity_intraday_low_paisa, trades_closed}`` points — so this DELEGATES to
    the ONE page projector rather than growing a second copy (§11 COMMON).

    Parameters
    ----------
    run_id, rank
        The requested coordinates (echoed).
    page
        The reader's equity page verdict.
    offset, limit
        The requested page window (echoed).
    fixture_root_active
        Stamped into the payload (C-MINOR-1).

    Returns
    -------
    dict
        The :func:`build_trades_payload` shape over equity points.
    """
    return build_trades_payload(
        run_id, rank, page, offset=offset, limit=limit, fixture_root_active=fixture_root_active
    )


# ── R41 chain map ───────────────────────────────────────────────────────


def _stage_timing_names(manifest: ManifestResult) -> set[str]:
    """Extract the stage names recorded in ``per_stage_timings`` (dict OR list shape)."""
    if manifest.manifest is None:
        return set()
    timings = manifest.manifest.get("per_stage_timings")
    if isinstance(timings, dict):
        return {str(key) for key in timings}
    if isinstance(timings, list):
        return {
            str(entry["stage"]) for entry in timings if isinstance(entry, dict) and "stage" in entry
        }
    return set()


def _tripwire_stages(manifest: ManifestResult) -> set[str]:
    """Extract the chain stages NAMED by tripwire entries (dict ``stage`` key or substring)."""
    if manifest.manifest is None:
        return set()
    tripwires = manifest.manifest.get("tripwires")
    if not isinstance(tripwires, list):
        return set()
    named: set[str] = set()
    for entry in tripwires:
        if isinstance(entry, dict):
            stage = entry.get("stage")
            if isinstance(stage, str):
                named.add(stage)
        elif isinstance(entry, str):
            named.update(stage for stage in CHAIN_STAGES if stage in entry)
    return named


def chain_links(manifest: ManifestResult, recent_kinds: Sequence[str]) -> list[dict[str, Any]]:
    """Derive the R41 chain-map link states — ONLY from recorded evidence.

    Fixed stage order :data:`CHAIN_STAGES`; per link the state is derived
    mechanically: ``red`` (a tripwire names the stage) → ``green`` (a
    ``per_stage_timings`` entry records it) → ``amber`` (event kinds mention
    it but no timing yet) → ``pending`` (no evidence — NEVER green).

    Parameters
    ----------
    manifest
        The manifest read verdict (timings + tripwires source).
    recent_kinds
        Event ``kind`` strings seen in the tailed window.

    Returns
    -------
    list of dict
        One ``{stage, state, derived_from}`` per chain stage.
    """
    timings = _stage_timing_names(manifest)
    tripwired = _tripwire_stages(manifest)
    kinds_lower = [kind.lower() for kind in recent_kinds]
    links: list[dict[str, Any]] = []
    for stage in CHAIN_STAGES:
        if stage in tripwired:
            state, why = "red", f"a tripwire names stage {stage!r}"
        elif stage in timings:
            state, why = "green", "per_stage_timings records the stage"
        elif any(stage in kind for kind in kinds_lower):
            state, why = "amber", "event kinds mention the stage; no timing recorded yet"
        else:
            state, why = "pending", "no timing, tripwire, or event evidence yet"
        links.append({"stage": stage, "state": state, "derived_from": why})
    return links


# ── R44 #7 funnel reconcile ─────────────────────────────────────────────


def _funnel_entry(entry: Any) -> tuple[str, int] | None:
    """Validate one ``actual.funnel`` entry → ``(stage, count)`` or ``None``."""
    if not isinstance(entry, dict):
        return None
    stage = entry.get("stage")
    count = entry.get("count")
    if isinstance(stage, str) and isinstance(count, int) and not isinstance(count, bool):
        return stage, count
    return None


def _kill_entry(entry: Any) -> tuple[str, int] | None:
    """Validate one ``per_door_kills`` entry → ``(door, killed)`` or ``None``."""
    if not isinstance(entry, dict):
        return None
    door = entry.get("door")
    killed = entry.get("killed")
    if isinstance(door, str) and isinstance(killed, int) and not isinstance(killed, bool):
        return door, killed
    return None


def reconcile_funnel(manifest: ManifestResult) -> dict[str, Any]:
    """R44 check #7 — per-stage funnel arithmetic, reconciled to the digit.

    ``ok`` iff every ``stage_n == stage_{n-1} − kills_n`` AND
    ``enumerated == tested + skipped``. Missing/malformed keys ⇒
    ``ok=None`` with status ``awaiting``/``partial`` — never a fake verdict.
    An EMPTY ``actual.funnel`` also abstains (``ok=None``, awaiting): zero
    stages reconcile NOTHING, so a manifest written before the funnel counts
    land must never read a vacuous green ✓.
    A door/stage COUNT mismatch also abstains (``ok=None``): the P2 contract
    keeps unobservable doors ABSENT (keyed by name, not positionally tiled to
    funnel transitions), so a sparse ``per_door_kills`` is a legitimate real
    manifest — never a false FAIL; name-keyed pairing lands at
    merge-integration (hostile-round MINOR-1).

    Parameters
    ----------
    manifest
        The manifest read verdict carrying ``actual.funnel`` +
        ``per_door_kills`` + the ``actual`` count triple.

    Returns
    -------
    dict
        ``{"ok": True|False|None, "status": ..., "lines": [...]}`` — the
        arithmetic lines are display-ready strings.
    """
    if manifest.manifest is None:
        return {
            "ok": None,
            "status": "awaiting",
            "lines": [f"manifest {manifest.status} — funnel reconcile awaiting"],
        }
    body = manifest.manifest
    actual = body.get("actual")
    if not isinstance(actual, dict):
        return {"ok": None, "status": "awaiting", "lines": ["manifest.actual missing — awaiting"]}
    raw_funnel = actual.get("funnel")
    raw_kills = body.get("per_door_kills")
    enumerated = actual.get("enumerated")
    tested = actual.get("tested")
    skipped = actual.get("skipped")
    if (
        not isinstance(raw_funnel, list)
        or not isinstance(raw_kills, list)
        or not isinstance(enumerated, int)
        or isinstance(enumerated, bool)
        or not isinstance(tested, int)
        or isinstance(tested, bool)
        or not isinstance(skipped, int)
        or isinstance(skipped, bool)
    ):
        missing = [
            name
            for name, value in (
                ("actual.funnel", raw_funnel),
                ("per_door_kills", raw_kills),
            )
            if not isinstance(value, list)
        ] + [
            f"actual.{name}"
            for name, value in (
                ("enumerated", enumerated),
                ("tested", tested),
                ("skipped", skipped),
            )
            if not isinstance(value, int) or isinstance(value, bool)
        ]
        return {
            "ok": None,
            "status": "partial",
            "lines": ["funnel reconcile awaiting: missing/malformed " + ", ".join(missing)],
        }
    funnel: list[tuple[str, int]] = []
    for idx, raw in enumerate(raw_funnel):
        entry = _funnel_entry(raw)
        if entry is None:
            return {
                "ok": None,
                "status": "partial",
                "lines": [f"funnel reconcile awaiting: malformed actual.funnel[{idx}]"],
            }
        funnel.append(entry)
    kills: list[tuple[str, int]] = []
    for idx, raw in enumerate(raw_kills):
        entry = _kill_entry(raw)
        if entry is None:
            return {
                "ok": None,
                "status": "partial",
                "lines": [f"funnel reconcile awaiting: malformed per_door_kills[{idx}]"],
            }
        kills.append(entry)
    if not funnel:
        # Vacuous-pass refusal: an empty funnel has zero transitions AND a
        # trivially-true sum line reconciles NOTHING — abstain, never a
        # green "reconciles to the digit" on no evidence.
        return {
            "ok": None,
            "status": "awaiting",
            "lines": ["funnel reconcile awaiting: actual.funnel is empty — nothing to reconcile"],
        }
    lines: list[str] = []
    if len(kills) != max(0, len(funnel) - 1):
        # ABSENT-key law: a door the run core cannot observe is ABSENT, so a
        # sparse kills list is a LEGITIMATE shape — abstain (amber), never a
        # false red. Positional pairing needs exactly one door per transition.
        return {
            "ok": None,
            "status": "partial",
            "lines": [
                f"funnel reconcile awaiting: per_door_kills has {len(kills)} door(s) "
                f"but actual.funnel has {len(funnel)} stage(s) — doors may be "
                "legitimately ABSENT per contract; name-keyed pairing lands at "
                "merge-integration"
            ],
        }
    ok = True
    for idx in range(1, len(funnel)):
        prev_stage, prev_count = funnel[idx - 1]
        cur_stage, cur_count = funnel[idx]
        door, killed = kills[idx - 1]
        expected = prev_count - killed
        line_ok = cur_count == expected
        verdict = "✓" if line_ok else f"✗ (manifest says {cur_count})"
        lines.append(
            f"{prev_stage} {prev_count} − {door} kills {killed} = {expected} → "
            f"{cur_stage} {verdict}"
        )
        ok = ok and line_ok
    sum_ok = enumerated == tested + skipped
    lines.append(
        f"enumerated {enumerated} == tested {tested} + skipped {skipped} {'✓' if sum_ok else '✗'}"
    )
    return {"ok": ok and sum_ok, "status": "ok", "lines": lines}


# ── R44 VERIFY-ME truth table (5 states) ────────────────────────────────


def verify_checks(
    manifest: ManifestResult,
    chain: tuple[bool, int, int | None, str] | None,
    trades_available: bool,
    *,
    reconcile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the 7 R44 check rows under the 5-state truth table (C-M4/T-M1).

    States: ``pass | fail | ready | awaiting | not_wired``. Hard caps:
    operator-judged checks #3/#4 can NEVER exceed ``ready`` server-side (the
    operator's ack is a client-side visual toggle, explicitly NOT server
    truth); un-wired checks #1/#5/#6 can NEVER read ``pass``. Check #2's
    detail ALWAYS carries the assumed-algorithm caveat, and a clean chain
    over ZERO records (an empty events.jsonl — a run killed before the
    recorder's first write) reads ``awaiting``, never a vacuous ``pass``.

    Parameters
    ----------
    manifest
        The manifest read verdict (feeds check #7 via :func:`reconcile_funnel`).
    chain
        The chain verdict tuple from ``verify_events_chain`` — ``None`` when
        not verified for the current events revision (⇒ awaiting).
    trades_available
        Whether ANY trades source currently serves rows (the #1275
        results-depth sidecars in production, or the fixture override).
    reconcile
        An optional precomputed :func:`reconcile_funnel` verdict (so
        :func:`build_verify_payload` computes it ONCE — hostile-round NIT-3);
        ``None`` ⇒ computed here.

    Returns
    -------
    list of dict
        Seven ``{id, name, state, detail[, records, first_break_line]}`` rows.
    """
    checks: list[dict[str, Any]] = [
        {
            "id": 1,
            "name": "Re-run determinism",
            "state": "not_wired",
            "detail": "not wired yet — lands at merge-integration (P2)",
        }
    ]
    if chain is None:
        checks.append(
            {
                "id": 2,
                "name": "Audit chain replay",
                "state": "awaiting",
                "detail": f"chain not verified yet — click Verify {_CHAIN_ASSUMED}",
                "records": None,
                "first_break_line": None,
            }
        )
    else:
        chain_ok, records, first_break, chain_detail = chain
        if chain_ok and records == 0:
            # Vacuous-pass refusal: an EMPTY events.jsonl replays "clean"
            # having verified NOTHING — awaiting evidence, never a green PASS.
            checks.append(
                {
                    "id": 2,
                    "name": "Audit chain replay",
                    "state": "awaiting",
                    "detail": (
                        "chain intact but 0 records — an empty events file verifies "
                        f"nothing {_CHAIN_ASSUMED}"
                    ),
                    "records": 0,
                    "first_break_line": first_break,
                }
            )
        else:
            checks.append(
                {
                    "id": 2,
                    "name": "Audit chain replay",
                    "state": "pass" if chain_ok else "fail",
                    "detail": chain_detail,
                    "records": records,
                    "first_break_line": first_break,
                }
            )
    if trades_available:
        eyeball_state = "ready"
        eyeball_detail = (
            "wired — operator eyeballs the chart vs Groww/TradingView (operator-judged, "
            "never auto-passes server-side)"
        )
        claim_state = "ready"
        claim_detail = (
            "wired — the expand sums the trade pages loaded below; a partially-loaded "
            "claim scopes itself to the N trades loaded (operator-judged)"
        )
    else:
        eyeball_state = "awaiting"
        eyeball_detail = "awaiting trades — no results-depth sidecars for this run yet"
        claim_state = "awaiting"
        claim_detail = "awaiting trades — no results-depth sidecars for this run yet"
    checks.append(
        {"id": 3, "name": "Trade → chart eyeball", "state": eyeball_state, "detail": eyeball_detail}
    )
    checks.append(
        {
            "id": 4,
            "name": "Claim → line-by-line arithmetic",
            "state": claim_state,
            "detail": claim_detail,
        }
    )
    checks.append(
        {
            "id": 5,
            "name": "Kill/resume same numbers",
            "state": "not_wired",
            "detail": "not wired yet — lands at merge-integration (P2 checkpoint/resume)",
        }
    )
    checks.append(
        {
            "id": 6,
            "name": "Re-click → 0 recomputed",
            "state": "not_wired",
            "detail": "not wired yet — lands at merge-integration (R6 dedup counts)",
        }
    )
    if reconcile is None:
        reconcile = reconcile_funnel(manifest)
    if reconcile["ok"] is True:
        funnel_state, funnel_detail = "pass", "funnel reconciles to the digit"
    elif reconcile["ok"] is False:
        funnel_state, funnel_detail = "fail", "funnel does NOT reconcile — see the lines"
    else:
        funnel_state, funnel_detail = "awaiting", "manifest funnel counts awaiting"
    checks.append(
        {
            "id": 7,
            "name": "Funnel reconciles to the digit",
            "state": funnel_state,
            "detail": funnel_detail,
        }
    )
    return checks


def build_verify_payload(
    run_id: str,
    manifest: ManifestResult,
    chain: tuple[bool, int, int | None, str] | None,
    trades_available: bool,
    *,
    fixture_root_active: bool,
) -> dict[str, Any]:
    """Assemble the ``GET /api/v1/oneclick/verify`` payload (checks + reconcile).

    Parameters
    ----------
    run_id
        The requested run id (echoed).
    manifest
        The manifest read verdict.
    chain
        The chain verdict (``None`` = not verified at this events revision).
    trades_available
        Whether a trades source currently serves rows.
    fixture_root_active
        Stamped into the payload (C-MINOR-1).

    Returns
    -------
    dict
        ``{run_id, checks[], funnel_reconcile{}, fixture_root_active}``.
    """
    reconcile = reconcile_funnel(manifest)
    return {
        "run_id": run_id,
        "checks": verify_checks(manifest, chain, trades_available, reconcile=reconcile),
        "funnel_reconcile": {"ok": reconcile["ok"], "lines": reconcile["lines"]},
        "fixture_root_active": fixture_root_active,
    }
