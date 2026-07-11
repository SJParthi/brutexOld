"""The unified operator HEALTH HUB fold (DEC-OPERATOR-HEALTH-TRUTH-001, MIGRATION.md #1747).

One read-only green/red fold over the automation lanes that previously had NO
dashboard surface — each lane read from DURABLE LOCAL state that ALREADY exists
(imported verdict code, never a copy — CLAUDE.md §11 COMMON):

* ``pull`` — the honest last-pull outcome
  (:func:`brutex.dashboard.pull_outcome.classify_last_pull_outcome` via an
  injected probe over the app's own job slot + verdict holder);
* ``nightly`` — the newest ``<results_root>/nightly/<id>/report.json`` written
  by the R9 nightly chain (DEC-SWEEP-NIGHTLY-AUTOMATION-001 — the frozen
  ``brutex-nightly-report-v1`` contract whose header says "the future
  dashboard card reads it": this is that card's data source);
* ``backup_deadman`` — the PURE O(1) dead-man verdict
  :func:`brutex.backup.deadman.evaluate_deadman` over the backup runner's
  ``state.json`` + the checker sidecar (DEC-BACKUP-OFFBOX-001) — READ-ONLY:
  this hub never writes the checker-owned sidecar and never alerts (the hourly
  ``brutex backup check`` remains the one alerting checker);
* ``doctor_daily`` / ``mutation`` — HONESTLY NOT WIRED: both gates run in
  GitHub Actions (``doctor-daily.yml`` 07:30 IST; ``mutation.yml`` weekly) and
  write NO local verdict artifact on this box, so the lanes read the explicit
  ``not_wired`` state — NEVER a fabricated green (the honesty law).

## Lane verdict vocabulary (stable machine tokens)

``ok`` (green — a POSITIVE recorded signal) · ``warn`` (amber — degraded /
overdue / suspect) · ``bad`` (red — failed / halted / alarm) · ``unknown``
(a signal SHOULD exist but is missing/unreadable — corrupt state reads a NAMED
degrade, never a 500 and never green) · ``idle`` (an honest neutral — e.g. no
pull has run in this process; non-degrading) · ``not_wired`` (no local signal
EXISTS by design — rendered visibly grey, non-degrading).

## The fold law

The hub verdict is ``bad`` iff ANY lane is ``bad``; else ``warn`` iff ANY lane
is ``warn`` OR ``unknown`` (an unreadable signal must look degraded); else
``ok``.  ``idle`` and ``not_wired`` lanes never degrade the fold — they are
neutral by construction — but they are always VISIBLE per-lane and the fold
headline counts them (``"N ok · M not wired"``), so a permanently-unwired lane
can never silently inflate a green.

## Honest O-envelope (Golden Rule #10)

A poll within ``ttl_s`` (default 5 s) serves the memoised file lanes — O(1)
dict work plus the O(1) in-memory pull-lane classify (recomputed per poll so a
pull halt is never TTL-delayed).  A refresh costs ONE ``scandir`` of
``<results_root>/nightly/`` (O(retained nightly-run dirs) NAMES — one dir per
night; no file content is read per entry) + one bounded ``report.json`` read
(≤ :data:`MAX_REPORT_BYTES`) + two tiny backup JSON reads (``state.json`` /
``deadman.json``, each < 2 KiB).  NEVER a lake scan, never a vendor call, never
a network touch.  This is O(1)-per-poll in lake size but NOT literal O(1)-total:
the nightly dir listing grows with retained nightly runs (stated, not hidden).
Thread-safe with a NON-BLOCKING follower discipline (the #1522 rate-bridge
posture): while one poller refreshes, a concurrent poll serves the LAST-KNOWN
file lanes instead of queueing — a wedged lake volume strands AT MOST ONE
threadpool worker, never a convoy (the stranded worker is the disclosed
residual).  The route runs :meth:`HealthHub.payload` on a threadpool worker so
the event loop never touches the filesystem.
READ-side observability only — ENGINE UNCHANGED (NIFTY/BANKNIFTY/SENSEX).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog

from brutex.backup.deadman import DeadmanVerdict, evaluate_deadman
from brutex.backup.errors import BackupStateError
from brutex.backup.state import deadman_path as backup_deadman_path
from brutex.backup.state import load_deadman, load_state, state_path
from brutex.config.backup_env import resolve_backup_env
from brutex.dashboard.pull_outcome import (
    TONE_BAD,
    TONE_OK,
    TONE_WARN,
    LastPullOutcome,
)
from brutex.nightly.report import NIGHTLY_DIRNAME, REPORT_FILENAME

_LOG = structlog.get_logger(__name__)

#: Lane verdict tokens (see the module docstring for the fold law).
VERDICT_OK: Final[str] = "ok"
VERDICT_WARN: Final[str] = "warn"
VERDICT_BAD: Final[str] = "bad"
VERDICT_UNKNOWN: Final[str] = "unknown"
VERDICT_IDLE: Final[str] = "idle"
VERDICT_NOT_WIRED: Final[str] = "not_wired"

#: Lane name tokens (the wire ``lane`` field, stable).
LANE_PULL: Final[str] = "pull"
LANE_NIGHTLY: Final[str] = "nightly"
LANE_BACKUP: Final[str] = "backup_deadman"
LANE_DOCTOR: Final[str] = "doctor_daily"
LANE_MUTATION: Final[str] = "mutation"

#: Bounded ``report.json`` read — a real nightly report is a few KiB; anything
#: beyond this is treated as corrupt (fail-closed, never an unbounded read).
MAX_REPORT_BYTES: Final[int] = 1_048_576  # 1 MiB

#: Nightly freshness threshold: the chain runs once per night, so a green
#: report older than this reads WARN "overdue".  30 h mirrors the backup
#: dead-man's default threshold — a POLICY heuristic (Golden Rule #10), not a
#: vendor fact; operator-revisable in this one Final.
NIGHTLY_STALE_HOURS: Final[float] = 30.0

#: File-lane memo TTL — a poll inside it is an O(1) dict serve.
DEFAULT_TTL_S: Final[float] = 5.0

#: Pull-outcome tone → lane verdict (the pull lane's honest neutral maps to
#: the non-degrading ``idle``, never a fabricated green).
_TONE_VERDICTS: Final[dict[str, str]] = {
    TONE_OK: VERDICT_OK,
    TONE_WARN: VERDICT_WARN,
    TONE_BAD: VERDICT_BAD,
}


@dataclass(frozen=True, slots=True)
class LaneStatus:
    """One lane's evaluated status (immutable, JSON-friendly).

    Attributes
    ----------
    lane
        The stable lane token (``pull`` / ``nightly`` / ``backup_deadman`` /
        ``doctor_daily`` / ``mutation``).
    verdict
        One of the six lane verdict tokens (module docstring).
    headline
        Operator-facing one-liner (rendered via ``textContent`` only).
    detail
        Supporting evidence / remediation line (may be ``""``).
    checked_at
        Epoch seconds of the underlying SIGNAL (the nightly report's
        ``ended_utc`` / the backup's last success stamp), or ``None`` when the
        lane has no timestamped signal — never a fabricated stamp.
    age_s
        ``now − checked_at`` (clamped ≥ 0), or ``None`` with it.
    """

    lane: str
    verdict: str
    headline: str
    detail: str
    checked_at: float | None
    age_s: float | None

    def to_dict(self) -> dict[str, Any]:
        """Return the wire dict (6 stable keys)."""
        return {
            "lane": self.lane,
            "verdict": self.verdict,
            "headline": self.headline,
            "detail": self.detail,
            "checked_at": self.checked_at,
            "age_s": self.age_s,
        }


def fold_verdict(lanes: list[LaneStatus]) -> str:
    """The documented fold law: bad > (warn | unknown) > ok; idle/not_wired neutral."""
    verdicts = {lane.verdict for lane in lanes}
    if VERDICT_BAD in verdicts:
        return VERDICT_BAD
    if VERDICT_WARN in verdicts or VERDICT_UNKNOWN in verdicts:
        return VERDICT_WARN
    return VERDICT_OK


def _fold_headline(lanes: list[LaneStatus]) -> str:
    """Honest fold headline counting every verdict class (nothing hidden)."""
    counts: dict[str, int] = {}
    for lane in lanes:
        counts[lane.verdict] = counts.get(lane.verdict, 0) + 1
    order = (VERDICT_BAD, VERDICT_WARN, VERDICT_UNKNOWN, VERDICT_OK, VERDICT_IDLE)
    parts = [f"{counts[v]} {v}" for v in order if v in counts]
    if VERDICT_NOT_WIRED in counts:
        parts.append(f"{counts[VERDICT_NOT_WIRED]} not wired")
    return " · ".join(parts)


def _pull_lane(outcome: LastPullOutcome) -> LaneStatus:
    """Project the (already-classified) last-pull outcome onto the pull lane."""
    verdict = _TONE_VERDICTS.get(outcome.tone, VERDICT_IDLE)
    return LaneStatus(
        lane=LANE_PULL,
        verdict=verdict,
        headline=outcome.headline,
        detail=outcome.detail,
        # The job snapshot's clock is monotonic (not wall) — no epoch stamp
        # exists for the in-memory outcome, so none is fabricated.
        checked_at=None,
        age_s=None,
    )


def _not_wired_lane(lane: str, headline: str, detail: str) -> LaneStatus:
    """An honest ``not_wired`` lane (no local signal EXISTS by design)."""
    return LaneStatus(
        lane=lane,
        verdict=VERDICT_NOT_WIRED,
        headline=headline,
        detail=detail,
        checked_at=None,
        age_s=None,
    )


def _doctor_lane() -> LaneStatus:
    """doctor-daily: a GitHub-Actions-only gate — no local artifact on this box."""
    return _not_wired_lane(
        LANE_DOCTOR,
        "not wired — no local doctor verdict artifact",
        "the daily doctor runs in GitHub Actions (doctor-daily.yml, 07:30 IST) "
        "and files/close its own tracking issue; `brutex doctor` writes no local verdict file",
    )


def _mutation_lane() -> LaneStatus:
    """mutation gate: a GitHub-Actions-only gate — no local artifact on this box."""
    return _not_wired_lane(
        LANE_MUTATION,
        "not wired — no local mutation-gate artifact",
        "the deep mutation gate runs in GitHub Actions (mutation.yml, weekly Mon 04:00 UTC) "
        "and files its own alert issues; no local verdict file exists",
    )


def _unknown_lane(lane: str, headline: str, detail: str) -> LaneStatus:
    """A NAMED unreadable-signal degrade (folds WARN — degraded, never a 500)."""
    return LaneStatus(
        lane=lane,
        verdict=VERDICT_UNKNOWN,
        headline=headline,
        detail=detail,
        checked_at=None,
        age_s=None,
    )


def _latest_nightly_id(nightly_dir: Path) -> str | None:
    """The newest nightly run id under *nightly_dir*, or ``None``.

    Nightly ids are ``nightly-YYYYMMDDTHHMMSSZ-<hex>`` (UTC-stamped —
    :func:`brutex.nightly.report.mint_nightly_id`), so the lexicographic max IS
    the newest.  One ``scandir`` of names — O(retained nightly dirs), no file
    content read.  Any listing error returns ``None`` (the caller degrades).
    """
    try:
        candidates = [
            entry.name
            for entry in nightly_dir.iterdir()
            if entry.is_dir() and entry.name.startswith("nightly-")
        ]
    except OSError:
        return None
    return max(candidates) if candidates else None


def _read_report_json(report_file: Path) -> Mapping[str, Any] | str:
    """Read + parse a nightly ``report.json`` (bounded); a defect returns the REASON string."""
    try:
        raw = report_file.read_bytes()
    except FileNotFoundError:
        return "latest nightly dir has no report.json (run crashed before the report write?)"
    except OSError as exc:
        return f"report.json unreadable: {type(exc).__name__}"
    if len(raw) > MAX_REPORT_BYTES:
        return f"report.json oversized ({len(raw)} bytes > {MAX_REPORT_BYTES} cap) — treated as corrupt"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return f"report.json corrupt: {type(exc).__name__}"
    if not isinstance(data, dict):
        return f"report.json is not a JSON object (got {type(data).__name__})"
    return data


def nightly_lane(
    results_root: Path | None,
    *,
    now: float,
    stale_hours: float = NIGHTLY_STALE_HOURS,
) -> LaneStatus:
    """Evaluate the nightly-chain lane from the newest durable ``report.json``.

    Verdict law: missing lake/nightly tree → ``not_wired`` (the nightly has
    never produced a report on this box); an unreadable/corrupt report →
    ``unknown`` naming the defect; ``ok`` report older than *stale_hours* →
    ``warn`` overdue; ``suspect`` (tripwires) → ``warn``; a breaker → ``bad``;
    else ``ok`` (a dry-run is green with an honest "(dry run)" note — the
    chain itself completed).
    """
    if results_root is None:
        return _not_wired_lane(
            LANE_NIGHTLY,
            "not wired — no lake root resolved",
            "nightly artifacts live under <lake>/sweeps/nightly/; no lake root is wired",
        )
    nightly_dir = results_root / NIGHTLY_DIRNAME
    latest = _latest_nightly_id(nightly_dir)
    if latest is None:
        return _not_wired_lane(
            LANE_NIGHTLY,
            "not wired — no nightly report on this box",
            "the R9 nightly chain (`brutex nightly run`) has not produced a report here",
        )
    report = _read_report_json(nightly_dir / latest / REPORT_FILENAME)
    if isinstance(report, str):
        return _unknown_lane(LANE_NIGHTLY, f"nightly report unreadable — {latest}", report)
    ended = report.get("ended_utc")
    checked_at = float(ended) if isinstance(ended, (int, float)) else None
    age_s = max(0.0, now - checked_at) if checked_at is not None else None
    ok = report.get("ok")
    suspect = report.get("suspect")
    blocker = report.get("blocker")
    if not isinstance(ok, bool) or not isinstance(suspect, bool):
        return _unknown_lane(
            LANE_NIGHTLY,
            f"nightly report unreadable — {latest}",
            "report.json lacks the boolean ok/suspect fields (schema drift?)",
        )
    dry_note = " (dry run)" if report.get("dry_run") is True else ""
    if ok:
        if age_s is not None and age_s > stale_hours * 3600.0:
            return LaneStatus(
                lane=LANE_NIGHTLY,
                verdict=VERDICT_WARN,
                headline=f"nightly OVERDUE — last ok {age_s / 3600.0:.1f} h ago",
                detail=f"{latest}{dry_note} — threshold {stale_hours:g} h "
                "(the chain runs once per night)",
                checked_at=checked_at,
                age_s=age_s,
            )
        return LaneStatus(
            lane=LANE_NIGHTLY,
            verdict=VERDICT_OK,
            headline=f"nightly ok — {latest}{dry_note}",
            detail="every link green, zero tripwires",
            checked_at=checked_at,
            age_s=age_s,
        )
    if suspect and not isinstance(blocker, str):
        # Tripwires fired with NO breaking link — SUSPECT (exit ≥ 2, cron-red).
        return LaneStatus(
            lane=LANE_NIGHTLY,
            verdict=VERDICT_WARN,
            headline=f"nightly SUSPECT — {latest}",
            detail="tripwire(s) fired — inspect the nightly bundle before trusting the run",
            checked_at=checked_at,
            age_s=age_s,
        )
    blocker_text = blocker if isinstance(blocker, str) else "unknown link"
    return LaneStatus(
        lane=LANE_NIGHTLY,
        verdict=VERDICT_BAD,
        headline=f"nightly FAILED — blocker: {blocker_text}",
        detail=f"{latest}{dry_note} — see <results>/nightly/{latest}/report.json",
        checked_at=checked_at,
        age_s=age_s,
    )


def backup_deadman_lane(
    *,
    now: float,
    env: Mapping[str, str] | None = None,
) -> LaneStatus:
    """Evaluate the backup dead-man lane via the PURE existing checker core.

    Reuses :func:`brutex.backup.deadman.evaluate_deadman` VERBATIM (CLAUDE.md
    §11 COMMON) over the runner's ``state.json`` + the checker sidecar —
    READ-ONLY (the sidecar is never written; no alert is published here; the
    hourly ``brutex backup check`` stays the one alerting checker).

    Verdict law: ``FRESH``/``RUNNING`` → ``ok`` (RUNNING is deliberately
    non-alarming — F17b); every alarm verdict (``OVERDUE``/``HUNG``/
    ``NEVER_SUCCEEDED``/``STATE_CORRUPT``/``CLOCK_SKEW``) → ``bad``.  ONE
    honest carve-out: an UNCONFIGURED lane (no ``BRUTEX_BACKUP_REPO``, no
    state file) reads ``not_wired`` instead of the checker's NEVER_SUCCEEDED —
    the dashboard hub must distinguish "never installed on this box" from
    "installed and silent" (the checker, which only runs where the lane IS
    installed, keeps its silence-equals-alarm law untouched).
    """
    cfg = resolve_backup_env(env)
    state_error: str | None = None
    try:
        state = load_state(state_path(cfg))
    except BackupStateError as exc:
        state = None
        state_error = str(exc)
    if state is None and state_error is None and cfg.repo is None:
        return _not_wired_lane(
            LANE_BACKUP,
            "not wired — off-box backup not configured",
            "BRUTEX_BACKUP_REPO is unset and no backup state file exists on this box",
        )
    sidecar = load_deadman(backup_deadman_path(cfg))
    report = evaluate_deadman(
        state,
        sidecar,
        threshold_hours=cfg.deadman_hours,
        realert_hours=cfg.realert_hours,
        skew_tolerance_s=cfg.clock_skew_tolerance_s,
        max_runtime_hours=cfg.max_runtime_hours,
        clock=lambda: datetime.fromtimestamp(now, UTC),
        state_error=state_error,
    )
    hours = report.hours_since_success
    age_s = max(0.0, hours * 3600.0) if hours is not None else None
    checked_at = (now - age_s) if age_s is not None else None
    if report.verdict in (DeadmanVerdict.FRESH, DeadmanVerdict.RUNNING):
        return LaneStatus(
            lane=LANE_BACKUP,
            verdict=VERDICT_OK,
            headline=f"backup {report.verdict.value}",
            detail=report.detail,
            checked_at=checked_at,
            age_s=age_s,
        )
    return LaneStatus(
        lane=LANE_BACKUP,
        verdict=VERDICT_BAD,
        headline=f"backup dead-man ALARM — {report.verdict.value}",
        detail=report.detail,
        checked_at=checked_at,
        age_s=age_s,
    )


class HealthHub:
    """TTL-memoised assembler of the health-hub payload (thread-safe, read-only).

    Parameters
    ----------
    lake_root_resolver
        Zero-arg resolver of the lake root (``None`` = no lake wired) — the
        nightly lane derives ``results_root = <lake>/sweeps`` from it (the
        ``cmd_sweep_all`` law).
    pull_outcome_probe
        Zero-arg probe returning the app's live
        :class:`~brutex.dashboard.pull_outcome.LastPullOutcome` — computed
        fresh per poll (O(1) in-memory), never TTL-delayed.
    env
        Environment mapping for the backup lane (default ``os.environ`` inside
        :func:`~brutex.config.backup_env.resolve_backup_env`); injected for tests.
    clock
        Epoch-seconds clock (default :func:`time.time`); injected for tests.
    ttl_s
        File-lane memo TTL (default :data:`DEFAULT_TTL_S`).
    """

    def __init__(
        self,
        *,
        lake_root_resolver: Callable[[], Path | None],
        pull_outcome_probe: Callable[[], LastPullOutcome],
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        self._lake_root_resolver = lake_root_resolver
        self._pull_outcome_probe = pull_outcome_probe
        self._env = env
        self._clock = clock
        self._ttl_s = ttl_s
        self._lock = threading.Lock()
        self._file_lanes: list[LaneStatus] | None = None
        self._file_lanes_at = float("-inf")
        #: EMIT-ON-CHANGE degrade-log signature (instance state, never a module
        #: global — CLAUDE.md §4): the (lane, verdict, headline) tuple of the
        #: last refresh's degraded lanes, so a persistently-corrupt state file
        #: logs ONE WARN on the transition (not one per 5 s TTL refresh) and
        #: recovery logs ONE INFO. Mutated only under :attr:`_lock`.
        self._degraded_sig: tuple[tuple[str, str, str], ...] = ()

    def _refresh_file_lanes(self, now: float) -> list[LaneStatus]:
        """Recompute the durable-file lanes (nightly + backup); memoised under TTL.

        EMIT-ON-CHANGE observability: a lane entering/changing a ``bad`` /
        ``unknown`` verdict logs ONE ``health_hub.lane_degraded`` WARN naming
        every currently-degraded lane; all-clear after a degraded period logs
        ONE ``health_hub.lanes_recovered`` INFO. A steady state (degraded or
        healthy) logs nothing — bounded noise, never a per-poll storm.
        Called with :attr:`_lock` HELD (the signature mutation rides it).
        """
        lake_root = self._lake_root_resolver()
        results_root = None if lake_root is None else lake_root / "sweeps"
        lanes = [
            nightly_lane(results_root, now=now),
            backup_deadman_lane(now=now, env=self._env),
        ]
        degraded = tuple(
            (lane.lane, lane.verdict, lane.headline)
            for lane in lanes
            if lane.verdict in (VERDICT_BAD, VERDICT_UNKNOWN)
        )
        if degraded != self._degraded_sig:
            if degraded:
                _LOG.warning(
                    "health_hub.lane_degraded",
                    lanes=[
                        {"lane": name, "verdict": verdict, "headline": headline}
                        for name, verdict, headline in degraded
                    ],
                )
            else:
                _LOG.info("health_hub.lanes_recovered")
            self._degraded_sig = degraded
        return lanes

    def _cold_pending_lanes(self) -> list[LaneStatus]:
        """The honest COLD-follower degrade: a refresh is in flight elsewhere.

        Served only in the narrow window where a FIRST refresh is still running
        on another poller's worker and nothing has ever been memoised — an
        ``unknown`` never a fabricated verdict; the next poll after the refresh
        lands serves the real lanes.
        """
        return [
            _unknown_lane(
                lane,
                "health probe in flight",
                "the first file-lane refresh has not completed yet — retrying next poll",
            )
            for lane in (LANE_NIGHTLY, LANE_BACKUP)
        ]

    def payload(self) -> dict[str, Any]:
        """Assemble the hub payload — the route's one entry point.

        The file lanes ride the TTL memo; the pull lane + the two ``not_wired``
        constants + the fold recompute per call (all O(1)).  Never raises for a
        degraded lane (a corrupt state file is a NAMED ``unknown``/alarm lane).

        NON-BLOCKING follower discipline (the rate-bridge posture): the memo
        lock is taken with ``blocking=False`` — while one poller's threadpool
        worker performs the (small, bounded) file refresh, a CONCURRENT poll
        never queues behind it: it serves the LAST-KNOWN file lanes (or the
        honest cold ``unknown`` pending lanes before the first refresh ever
        lands), so a wedged lake volume can strand AT MOST ONE worker on the
        refresh — never a convoy of blocked pollers (the #999/#1522 stance;
        the stranded worker is the disclosed residual, not denied).
        """
        now = self._clock()
        file_lanes: list[LaneStatus]
        if self._lock.acquire(blocking=False):
            try:
                if self._file_lanes is None or (now - self._file_lanes_at) >= self._ttl_s:
                    self._file_lanes = self._refresh_file_lanes(now)
                    self._file_lanes_at = now
                file_lanes = list(self._file_lanes)
            finally:
                self._lock.release()
        else:
            # A refresh (or another poll) holds the memo — serve last-known
            # WITHOUT blocking. ``_file_lanes`` is only ever REPLACED (never
            # mutated in place) under the lock, so this unlocked read is a
            # torn-state-free reference grab (CPython atomic attribute read).
            known = self._file_lanes
            file_lanes = list(known) if known is not None else self._cold_pending_lanes()
        lanes = [
            _pull_lane(self._pull_outcome_probe()),
            *file_lanes,
            _doctor_lane(),
            _mutation_lane(),
        ]
        return {
            "fold": {"verdict": fold_verdict(lanes), "headline": _fold_headline(lanes)},
            "lanes": [lane.to_dict() for lane in lanes],
            "generated_at": now,
            "ttl_s": self._ttl_s,
        }


__all__ = [
    "DEFAULT_TTL_S",
    "LANE_BACKUP",
    "LANE_DOCTOR",
    "LANE_MUTATION",
    "LANE_NIGHTLY",
    "LANE_PULL",
    "MAX_REPORT_BYTES",
    "NIGHTLY_STALE_HOURS",
    "VERDICT_BAD",
    "VERDICT_IDLE",
    "VERDICT_NOT_WIRED",
    "VERDICT_OK",
    "VERDICT_UNKNOWN",
    "VERDICT_WARN",
    "HealthHub",
    "LaneStatus",
    "backup_deadman_lane",
    "fold_verdict",
    "nightly_lane",
]
