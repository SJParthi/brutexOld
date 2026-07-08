"""Run manifest — contract-exact writer/reader + preflight fold (R28, D-7/D-8/D-17).

Every sweep run persists ONE manifest at
``<results_root>/runs/<run_id>/manifest.json`` carrying the NINE frozen
contract keys (sweep-parallel-integration-contracts.md #3, field names EXACT)::

    {run_id, selection, estimate, actual, per_stage_timings, per_door_kills,
     dedup_skips, tripwires, suspect}

plus the ADDITIVE keys ``schema_version`` / ``attempt`` / ``exit_code`` /
``updated_utc`` / ``suspect_reasons`` / ``dedup_skips_detail`` / ``preflight``
/ ``recorder_degraded`` (design §3).

Shape laws
----------
* **dedup_skips is a scalar int** (D-7); the additive ``dedup_skips_detail``
  is ``{"cache": n, "resume": n, "sentinel": n}`` (exactly those three keys).
* **per_door_kills — ABSENT-key law (D-8):** a door the run core cannot
  observe is ABSENT — never ``null``, never a fabricated ``0`` (an OBSERVED
  zero is legitimately present). Only :data:`SOURCEABLE_DOORS` may appear;
  the 5 hard-gate splits stay absent pending their named follow-up DEC.
  On disk the doors serialize as the P4 sub-shape — an ORDERED
  ``[{"door": str, "killed": int}]`` list in canonical door order (the
  contracts memory tail section); :func:`load_manifest` also accepts the
  original mapping sketch for tolerance.
* **actual sub-shapes (P4 tail):** when present, ``actual.funnel`` is an
  ordered ``[{"stage": str, "count": int}]`` list and
  ``actual.enumerated`` / ``actual.tested`` / ``actual.skipped`` are ints —
  validated on write, never fabricated when absent.
* **estimate** is the P1 estimator fragment or an honest ``null`` until the
  P1 adapter is wired (D-17); :func:`fold_preflight` folds the FULL P1
  preflight-verdict contract (``refusals[]`` + ``clamps[]`` + ``estimator``).

Failure split: the in-memory :class:`ManifestAccumulator` mutation methods
fail LOUD on programmer errors (unknown door/dedup kind, negative counts —
``ValueError``); the I/O boundary is fail-soft where the run core needs it
(:meth:`ManifestAccumulator.flush` / :func:`write_manifest_guarded` → ONE
``run_manifest.write_failed`` WARN + ``None``, I-3). Writes are atomic via the
COMMON :func:`brutex.data.atomic_io.atomic_write_simple` (tmp → fsync →
``os.replace``) — a kill mid-write leaves the previous manifest intact, never
a torn one (failure matrix A12). ``updated_utc`` is observability-only wall
clock, never part of any identity (I-2).
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog

from brutex.data.atomic_io import atomic_write_simple
from brutex.engine.run_layout import run_dir
from brutex.models import BruteXError

_LOG = structlog.get_logger(__name__)

__all__ = [
    "DEDUP_KINDS",
    "MANIFEST_FILENAME",
    "MANIFEST_SCHEMA_VERSION",
    "SOURCEABLE_DOORS",
    "ManifestAccumulator",
    "RunManifest",
    "RunManifestError",
    "build_funnel",
    "fold_preflight",
    "load_manifest",
    "manifest_path",
    "write_manifest",
    "write_manifest_guarded",
]

#: Manifest schema version (design §3).
MANIFEST_SCHEMA_VERSION: Final[int] = 1

#: The manifest file name inside ``runs/<run_id>/`` (contracts file #3).
MANIFEST_FILENAME: Final[str] = "manifest.json"

#: The dedup-skip detail kinds (D-7) — exactly these three keys, always all present.
DEDUP_KINDS: Final[tuple[str, ...]] = ("cache", "resume", "sentinel")

#: The doors the run core can OBSERVE today (D-8), in canonical output order:
#: grid tallies (min_hits / no_bars / cache_skip / resume_skip) then the six
#: cascade doors. Anything else is a typo or an un-ledgered expansion — refused.
SOURCEABLE_DOORS: Final[tuple[str, ...]] = (
    "min_hits",
    "no_bars",
    "cache_skip",
    "resume_skip",
    "dsr",
    "spa",
    "cpcv",
    "walk_forward",
    "k_of_n",
    "robustness",
)

#: The four P1 preflight-verdict contract keys (contracts file #1); everything
#: else a verdict carries folds into the manifest ``selection`` fragment.
_PREFLIGHT_CONTRACT_KEYS: Final[frozenset[str]] = frozenset(
    {"ok", "refusals", "clamps", "estimator"}
)

#: The estimator fragment's required int fields (contracts file #1).
_ESTIMATOR_KEYS: Final[tuple[str, ...]] = ("trials", "est_seconds_provisional", "est_bytes")


class RunManifestError(BruteXError):
    """A manifest could not be serialised, written, or parsed."""


def _default_dedup_detail() -> dict[str, int]:
    """Fresh all-zero dedup detail — the D-7 three-key shape."""
    return dict.fromkeys(DEDUP_KINDS, 0)


def _default_preflight() -> dict[str, object]:
    """Fresh empty preflight fragment — the D-17 additive top-level key."""
    return {"refusals": [], "clamps": []}


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The contract-exact run manifest (contracts file #3 + design-§3 additives).

    The first nine fields are the FROZEN cross-phase contract keys; the rest
    are additive. ``per_door_kills`` holds observed doors ONLY (D-8 absent-key
    law); ``dedup_skips`` is the scalar total (D-7).
    """

    run_id: str
    selection: dict[str, object]
    estimate: dict[str, object] | None
    actual: dict[str, object]
    per_stage_timings: dict[str, float]
    per_door_kills: dict[str, int]
    dedup_skips: int
    tripwires: list[dict[str, object]]
    suspect: bool
    schema_version: int = MANIFEST_SCHEMA_VERSION
    attempt: int = 1
    exit_code: int | None = None
    updated_utc: float = 0.0
    suspect_reasons: tuple[str, ...] = ()
    dedup_skips_detail: dict[str, int] = field(default_factory=_default_dedup_detail)
    preflight: dict[str, object] = field(default_factory=_default_preflight)
    recorder_degraded: bool = False


def manifest_path(results_root: Path, run_id: str) -> Path:
    """Return the manifest path — ``<results_root>/runs/<run_id>/manifest.json``.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity (D-1).

    Returns
    -------
    Path
        The contract manifest location (contracts file #3).
    """
    return run_dir(results_root, run_id) / MANIFEST_FILENAME


def _is_int(value: object) -> bool:
    """``True`` iff *value* is a real int (bool excluded — JSON true/false trap)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require(condition: bool, message: str) -> None:
    """Raise :class:`RunManifestError` with *message* unless *condition* holds."""
    if not condition:
        raise RunManifestError(message)


def _doors_to_list(doors: Mapping[str, int]) -> list[dict[str, object]]:
    """Serialise observed doors as the ORDERED P4 sub-shape ``[{door, killed}]``.

    Canonical :data:`SOURCEABLE_DOORS` order; only PRESENT doors appear (D-8).
    """
    for door, killed in doors.items():
        _require(door in SOURCEABLE_DOORS, f"per_door_kills has unknown door {door!r}")
        _require(_is_int(killed) and killed >= 0, f"per_door_kills[{door!r}] must be an int >= 0")
    return [{"door": door, "killed": doors[door]} for door in SOURCEABLE_DOORS if door in doors]


def _doors_from_payload(raw: object) -> dict[str, int]:
    """Parse ``per_door_kills`` from EITHER the ordered-list or mapping shape."""
    doors: dict[str, int] = {}
    if isinstance(raw, Mapping):
        items = [(door, killed) for door, killed in raw.items()]
    elif isinstance(raw, list):
        items = []
        for row in raw:
            _require(
                isinstance(row, Mapping) and set(row.keys()) == {"door", "killed"},
                f"per_door_kills rows must be {{door, killed}} mappings, got {row!r}",
            )
            items.append((row["door"], row["killed"]))
    else:
        raise RunManifestError(
            f"per_door_kills must be a list or mapping, got {type(raw).__name__}"
        )
    for door, killed in items:
        _require(isinstance(door, str), f"per_door_kills door must be a str, got {door!r}")
        _require(_is_int(killed) and killed >= 0, f"per_door_kills[{door!r}] must be an int >= 0")
        doors[str(door)] = int(killed)
    return doors


def _validate_actual(actual: Mapping[str, object]) -> dict[str, object]:
    """Validate the ``actual`` fragment's P4 sub-shapes (never fabricate absent ones)."""
    out = dict(actual)
    funnel = out.get("funnel")
    if funnel is not None:
        if not isinstance(funnel, list):
            raise RunManifestError("actual.funnel must be an ordered list")
        for row in funnel:
            _require(
                isinstance(row, Mapping)
                and set(row.keys()) == {"stage", "count"}
                and isinstance(row["stage"], str)
                and _is_int(row["count"]),
                f"actual.funnel rows must be {{stage: str, count: int}}, got {row!r}",
            )
    for key in ("enumerated", "tested", "skipped"):
        if key in out:
            _require(_is_int(out[key]), f"actual.{key} must be an int")
    return out


def _to_payload(manifest: RunManifest) -> dict[str, object]:
    """Render *manifest* as the exact on-disk payload (contract keys first)."""
    _require(
        isinstance(manifest.run_id, str) and bool(manifest.run_id), "run_id must be a non-empty str"
    )
    _require(
        _is_int(manifest.dedup_skips) and manifest.dedup_skips >= 0,
        "dedup_skips must be an int >= 0",
    )
    _require(isinstance(manifest.suspect, bool), "suspect must be a bool")
    _require(
        set(manifest.dedup_skips_detail.keys()) == set(DEDUP_KINDS),
        f"dedup_skips_detail must carry exactly {DEDUP_KINDS}",
    )
    for kind, count in manifest.dedup_skips_detail.items():
        _require(_is_int(count) and count >= 0, f"dedup_skips_detail[{kind!r}] must be an int >= 0")
    for row in manifest.tripwires:
        _require(isinstance(row, Mapping), f"tripwires rows must be mappings, got {row!r}")
    return {
        "run_id": manifest.run_id,
        "selection": dict(manifest.selection),
        "estimate": None if manifest.estimate is None else dict(manifest.estimate),
        "actual": _validate_actual(manifest.actual),
        "per_stage_timings": {k: float(v) for k, v in manifest.per_stage_timings.items()},
        "per_door_kills": _doors_to_list(manifest.per_door_kills),
        "dedup_skips": manifest.dedup_skips,
        "tripwires": [dict(row) for row in manifest.tripwires],
        "suspect": manifest.suspect,
        "schema_version": manifest.schema_version,
        "attempt": manifest.attempt,
        "exit_code": manifest.exit_code,
        "updated_utc": manifest.updated_utc,
        "suspect_reasons": list(manifest.suspect_reasons),
        "dedup_skips_detail": dict(manifest.dedup_skips_detail),
        "preflight": dict(manifest.preflight),
        "recorder_degraded": manifest.recorder_degraded,
    }


def write_manifest(results_root: Path, m: RunManifest) -> Path:
    """Atomically write *m* to ``runs/<run_id>/manifest.json`` — fail-LOUD.

    Serialisation is RFC 8259-strict (``allow_nan=False`` — a NaN/Infinity
    float is refused, never written into a contract artifact) and the write is
    the COMMON atomic tmp→fsync→``os.replace`` (A12: a kill mid-write leaves
    the previous manifest intact).

    Parameters
    ----------
    results_root
        The sweep results root.
    m
        The manifest to persist.

    Returns
    -------
    Path
        The written manifest path.

    Raises
    ------
    RunManifestError
        On a shape violation, a non-finite float, or any write failure.
    """
    payload = _to_payload(m)
    try:
        rendered = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except ValueError as exc:
        raise RunManifestError(
            f"manifest for run {m.run_id!r} is not JSON-serialisable: {exc}"
        ) from exc
    dest = manifest_path(results_root, m.run_id)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_simple(dest, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    except OSError as exc:
        raise RunManifestError(f"failed to write manifest {dest}: {exc}") from exc
    return dest


def write_manifest_guarded(results_root: Path, m: RunManifest) -> Path | None:
    """The fail-soft twin of :func:`write_manifest` for ``finally`` paths (I-3).

    Any failure → ONE ``run_manifest.write_failed`` WARN + ``None`` — a
    manifest write can never kill the run (failure matrix A12/F9).

    Parameters
    ----------
    results_root
        The sweep results root.
    m
        The manifest to persist.

    Returns
    -------
    Path | None
        The written path, or ``None`` when the write failed.
    """
    try:
        return write_manifest(results_root, m)
    except Exception as exc:
        _LOG.warning(
            "run_manifest.write_failed",
            run_id=m.run_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def load_manifest(results_root: Path, run_id: str) -> RunManifest:
    """Load and validate the run's manifest — :class:`RunManifestError` on any defect.

    Accepts BOTH ``per_door_kills`` wire shapes (the canonical ordered list
    and the original mapping sketch). Additive keys absent in an older file
    load at their documented defaults — honest back-compat, never fabricated
    doors.

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The run identity.

    Returns
    -------
    RunManifest
        The parsed manifest.

    Raises
    ------
    RunManifestError
        Missing file, malformed JSON, or any contract-shape violation.
    """
    path = manifest_path(results_root, run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RunManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RunManifestError(f"malformed manifest JSON at {path}: {exc}") from exc
    _require(isinstance(raw, dict), f"manifest root must be an object, got {type(raw).__name__}")
    missing = [
        key
        for key in (
            "run_id",
            "selection",
            "estimate",
            "actual",
            "per_stage_timings",
            "per_door_kills",
            "dedup_skips",
            "tripwires",
            "suspect",
        )
        if key not in raw
    ]
    _require(not missing, f"manifest {path} missing contract keys: {missing}")
    _require(isinstance(raw["run_id"], str), "run_id must be a str")
    _require(isinstance(raw["selection"], Mapping), "selection must be a mapping")
    estimate_raw = raw["estimate"]
    _require(
        estimate_raw is None or isinstance(estimate_raw, Mapping),
        "estimate must be a mapping or null",
    )
    _require(isinstance(raw["actual"], Mapping), "actual must be a mapping")
    timings_raw = raw["per_stage_timings"]
    _require(isinstance(timings_raw, Mapping), "per_stage_timings must be a mapping")
    timings: dict[str, float] = {}
    for stage, seconds in timings_raw.items():
        _require(
            isinstance(seconds, int | float) and not isinstance(seconds, bool),
            f"per_stage_timings[{stage!r}] must be a number",
        )
        timings[str(stage)] = float(seconds)
    _require(
        _is_int(raw["dedup_skips"]) and raw["dedup_skips"] >= 0, "dedup_skips must be an int >= 0"
    )
    tripwires_raw = raw["tripwires"]
    _require(isinstance(tripwires_raw, list), "tripwires must be a list")
    for row in tripwires_raw:
        _require(isinstance(row, Mapping), f"tripwires rows must be mappings, got {row!r}")
    _require(isinstance(raw["suspect"], bool), "suspect must be a bool")
    detail_raw = raw.get("dedup_skips_detail", _default_dedup_detail())
    _require(
        isinstance(detail_raw, Mapping) and set(detail_raw.keys()) == set(DEDUP_KINDS),
        f"dedup_skips_detail must carry exactly {DEDUP_KINDS}",
    )
    detail: dict[str, int] = {}
    for kind in DEDUP_KINDS:
        count = detail_raw[kind]
        _require(_is_int(count) and count >= 0, f"dedup_skips_detail[{kind!r}] must be an int >= 0")
        detail[kind] = int(count)
    exit_code = raw.get("exit_code")
    _require(exit_code is None or _is_int(exit_code), "exit_code must be an int or null")
    reasons_raw = raw.get("suspect_reasons", [])
    _require(
        isinstance(reasons_raw, list) and all(isinstance(r, str) for r in reasons_raw),
        "suspect_reasons must be a list of str",
    )
    preflight_raw = raw.get("preflight", _default_preflight())
    _require(isinstance(preflight_raw, Mapping), "preflight must be a mapping")
    return RunManifest(
        run_id=raw["run_id"],
        selection=dict(raw["selection"]),
        estimate=None if estimate_raw is None else dict(estimate_raw),
        actual=_validate_actual(raw["actual"]),
        per_stage_timings=timings,
        per_door_kills=_doors_from_payload(raw["per_door_kills"]),
        dedup_skips=int(raw["dedup_skips"]),
        tripwires=[dict(row) for row in tripwires_raw],
        suspect=raw["suspect"],
        schema_version=int(raw.get("schema_version", MANIFEST_SCHEMA_VERSION)),
        attempt=int(raw.get("attempt", 1)),
        exit_code=None if exit_code is None else int(exit_code),
        updated_utc=float(raw.get("updated_utc", 0.0)),
        suspect_reasons=tuple(reasons_raw),
        dedup_skips_detail=detail,
        preflight=dict(preflight_raw),
        recorder_degraded=bool(raw.get("recorder_degraded", False)),
    )


def _as_list(value: object, *, key: str) -> list[object]:
    """Coerce a preflight list field fail-soft: junk → WARN + ``[]`` (honest)."""
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(value)
    _LOG.warning("run_manifest.preflight_field_malformed", key=key, value_type=type(value).__name__)
    return []


def fold_preflight(
    verdict: Mapping[str, object],
) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object]]:
    """Fold the FULL P1 preflight-verdict contract into manifest fragments (D-17).

    The P1 contract (contracts file #1) is ``{ok, refusals[], clamps[],
    estimator{trials, est_seconds_provisional, est_bytes}}``. Every OTHER key
    the verdict carries (echoed selection fields) folds into the selection
    fragment. A missing/None estimator yields an honest ``None`` estimate; a
    MALFORMED estimator (wrong shape / non-int fields) yields ``None`` + ONE
    ``run_manifest.estimator_malformed`` WARN — never a fabricated estimate
    (Golden Rule #1).

    Parameters
    ----------
    verdict
        The P1 preflight verdict mapping.

    Returns
    -------
    tuple[dict, dict | None, dict]
        ``(selection_fragment, estimate_or_None,
        preflight_fragment {refusals, clamps})``.
    """
    selection = {k: v for k, v in verdict.items() if k not in _PREFLIGHT_CONTRACT_KEYS}
    estimator = verdict.get("estimator")
    estimate: dict[str, object] | None = None
    if estimator is not None:
        if isinstance(estimator, Mapping) and all(
            _is_int(estimator.get(k)) for k in _ESTIMATOR_KEYS
        ):
            estimate = dict(estimator)
        else:
            _LOG.warning(
                "run_manifest.estimator_malformed",
                estimator=repr(estimator)[:200],
                required_keys=list(_ESTIMATOR_KEYS),
            )
    preflight: dict[str, object] = {
        "refusals": _as_list(verdict.get("refusals"), key="refusals"),
        "clamps": _as_list(verdict.get("clamps"), key="clamps"),
    }
    return selection, estimate, preflight


def build_funnel(counts: Mapping[str, int]) -> list[dict[str, object]]:
    """Build the ordered ``actual.funnel`` sub-shape from stage counts (P4 tail).

    Order is the mapping's insertion order (the caller's pipeline order).

    Parameters
    ----------
    counts
        Stage name → count (real observed ints, never fabricated).

    Returns
    -------
    list[dict[str, object]]
        Ordered ``[{"stage": str, "count": int}]`` rows.

    Raises
    ------
    ValueError
        On a non-str stage or a non-int / negative count (programmer error).
    """
    rows: list[dict[str, object]] = []
    for stage, count in counts.items():
        if not isinstance(stage, str):
            raise ValueError(f"funnel stage must be a str, got {stage!r}")
        if not _is_int(count) or count < 0:
            raise ValueError(f"funnel count for {stage!r} must be an int >= 0, got {count!r}")
        rows.append({"stage": stage, "count": count})
    return rows


class ManifestAccumulator:
    """Mutable per-run manifest builder — atomic rewrite per stage boundary.

    Mutation methods are in-memory, thread-safe (one internal lock — the R39
    tripwire fan-out may call :meth:`add_tripwire` off-thread) and fail LOUD
    on programmer errors; :meth:`flush` is the fail-soft I/O boundary (I-3).
    Timings use the injected *monotonic* clock; ``updated_utc`` uses the
    injected *wall* clock (observability-only, I-2).
    """

    def __init__(
        self,
        *,
        results_root: Path,
        run_id: str,
        selection: Mapping[str, object],
        estimate: Mapping[str, object] | None = None,
        attempt: int = 1,
        preflight: Mapping[str, object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ) -> None:
        """Seed the accumulator with the launch-time fragments.

        Parameters
        ----------
        results_root
            The sweep results root.
        run_id
            The run identity (D-1).
        selection
            The manifest ``selection`` fragment (who/when/exact selection).
        estimate
            The P1 estimator fragment, or ``None`` (honest null — D-17).
        attempt
            Observability-only attempt ordinal.
        preflight
            The D-17 ``{refusals, clamps}`` fragment (``None`` → empty).
        monotonic
            Duration clock (injected for tests).
        wall
            Wall clock for ``updated_utc`` (injected for tests).
        """
        self._results_root = results_root
        self._run_id = run_id
        self._selection = dict(selection)
        self._estimate = None if estimate is None else dict(estimate)
        self._attempt = attempt
        self._preflight = _default_preflight() if preflight is None else dict(preflight)
        self._monotonic = monotonic
        self._wall = wall
        self._lock = threading.Lock()
        self._stage_started: dict[str, float] = {}
        self._timings: dict[str, float] = {}
        self._actual: dict[str, object] = {}
        self._doors: dict[str, int] = {}
        self._dedup: dict[str, int] = _default_dedup_detail()
        self._tripwires: list[dict[str, object]] = []
        self._suspect = False
        self._suspect_reasons: list[str] = []
        self._exit_code: int | None = None
        self._recorder_degraded = False

    def stage_started(self, stage: str) -> None:
        """Mark *stage* as started (its timing runs until :meth:`stage_ended`)."""
        with self._lock:
            self._stage_started[stage] = self._monotonic()

    def stage_ended(self, stage: str) -> None:
        """Close *stage*'s timing window; re-entered stages accumulate.

        A ``stage_ended`` without a matching start is a WARNed no-op
        (``run_manifest.stage_not_started``) — never a crash, never a
        fabricated duration.
        """
        with self._lock:
            started = self._stage_started.pop(stage, None)
            if started is None:
                _LOG.warning("run_manifest.stage_not_started", stage=stage, run_id=self._run_id)
                return
            self._timings[stage] = self._timings.get(stage, 0.0) + (self._monotonic() - started)

    def set_actual(self, actual: Mapping[str, object]) -> None:
        """Replace the ``actual`` fragment (validated against the P4 sub-shapes).

        Raises
        ------
        RunManifestError
            On an invalid ``funnel`` / ``enumerated`` / ``tested`` /
            ``skipped`` sub-shape (programmer error — fail loud in memory).
        """
        validated = _validate_actual(actual)
        with self._lock:
            self._actual = validated

    def add_door_kills(self, door: str, killed: int) -> None:
        """Accumulate *killed* onto an OBSERVED door (D-8: only observed doors exist).

        Raises
        ------
        ValueError
            On an unknown door name or a non-int / negative count.
        """
        if door not in SOURCEABLE_DOORS:
            raise ValueError(
                f"unknown per_door_kills door {door!r}; sourceable: {SOURCEABLE_DOORS}"
            )
        if not _is_int(killed) or killed < 0:
            raise ValueError(f"door kill count must be an int >= 0, got {killed!r}")
        with self._lock:
            self._doors[door] = self._doors.get(door, 0) + killed

    def add_dedup_skip(self, kind: str, count: int = 1) -> None:
        """Accumulate a dedup skip of *kind* (``cache`` / ``resume`` / ``sentinel``).

        Raises
        ------
        ValueError
            On an unknown kind or a non-int / negative count.
        """
        if kind not in DEDUP_KINDS:
            raise ValueError(f"unknown dedup-skip kind {kind!r}; known: {DEDUP_KINDS}")
        if not _is_int(count) or count < 0:
            raise ValueError(f"dedup-skip count must be an int >= 0, got {count!r}")
        with self._lock:
            self._dedup[kind] += count

    def add_tripwire(self, event: Mapping[str, object]) -> None:
        """Append one tripwire event row (R39 → manifest ``tripwires[]``)."""
        row = dict(event)
        with self._lock:
            self._tripwires.append(row)

    def mark_suspect(self, reason: str) -> None:
        """Latch the run SUSPECT (D-12 — set-once, never cleared) naming *reason*."""
        with self._lock:
            self._suspect = True
            if reason not in self._suspect_reasons:
                self._suspect_reasons.append(reason)

    def set_exit_code(self, code: int | None) -> None:
        """Record the run's exit code (additive manifest key)."""
        with self._lock:
            self._exit_code = code

    def set_recorder_degraded(self, flag: bool) -> None:
        """Record the D-20 recorder-degraded flag (additive manifest key)."""
        with self._lock:
            self._recorder_degraded = flag

    def snapshot(self) -> RunManifest:
        """Build the current :class:`RunManifest` (``updated_utc`` = wall now)."""
        with self._lock:
            dedup_detail = dict(self._dedup)
            return RunManifest(
                run_id=self._run_id,
                selection=dict(self._selection),
                estimate=None if self._estimate is None else dict(self._estimate),
                actual=dict(self._actual),
                per_stage_timings=dict(self._timings),
                per_door_kills=dict(self._doors),
                dedup_skips=sum(dedup_detail.values()),
                tripwires=[dict(row) for row in self._tripwires],
                suspect=self._suspect,
                schema_version=MANIFEST_SCHEMA_VERSION,
                attempt=self._attempt,
                exit_code=self._exit_code,
                updated_utc=self._wall(),
                suspect_reasons=tuple(self._suspect_reasons),
                dedup_skips_detail=dedup_detail,
                preflight=dict(self._preflight),
                recorder_degraded=self._recorder_degraded,
            )

    def flush(self) -> Path | None:
        """Atomically rewrite the manifest — fail-soft (the I-3 I/O boundary).

        Returns
        -------
        Path | None
            The written path, or ``None`` on a WARNed failure.
        """
        return write_manifest_guarded(self._results_root, self.snapshot())
