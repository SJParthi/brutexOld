"""Sweep RUN-CORE manifest — the per-run forensic summary artifact.

DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1605). The run manifest is the
``manifest.json`` written under the run's SIBLING artifact directory
(:class:`brutex.engine.run_layout.RunLayout`) at run-end — the P2→P3/P4/P6
contract that lets a consumer reconstruct WHAT a sweep run selected, estimated,
actually did, where time went, which doors killed how many combos, how much
dedup was skipped, which tripwires fired, and whether the run is SUSPECT — none
of which exists anywhere on ``main`` today (gap #4 of ``sweep-gap-register-jul8``).

The NINE binding contract keys (``sweep-parallel-integration-contracts``)::

    run_id, selection, estimate, actual, per_stage_timings,
    per_door_kills, dedup_skips, tripwires[], suspect

plus the additive keys ``schema_version, attempt, exit_code, updated_utc,
suspect_reasons[], dedup_skips_detail, preflight, recorder_degraded``.

Two BINDING laws (contract-fixture-pinned):

* **D-7 — ``dedup_skips`` is a SCALAR int**, and the additive
  ``dedup_skips_detail`` = ``{cache, resume, sentinel}`` ints carry the
  breakdown. The scalar is the headline; the detail is the (possibly partial)
  attribution — they are NOT forced equal (an unobservable channel stays 0
  without lying about the headline).
* **D-8 — ``per_door_kills`` ABSENT-key law**: a door the run core CANNOT
  observe is **ABSENT** from the mapping — never ``null``, never a fabricated
  ``0`` (a consumer MUST distinguish *absent* = unobservable from *0* =
  observed-and-killed-nothing). On disk ``per_door_kills`` serialises as the
  ordered ``[{door, killed}]`` list in :data:`CANONICAL_DOOR_ORDER` (the P4
  sub-shape); an absent door has NO row. Populated today: ``min_hits``,
  ``no_bars``, ``cache_skip``, ``resume_skip`` + the six cascade doors
  (``dsr/spa/cpcv/walk_forward/k_of_n/robustness``); the five hard-gate splits
  are present in the canonical ORDER but stay ABSENT from the DATA until P2
  folds the ``brutex.ranking.gate_reasons`` counters (Ruling 12, named
  follow-up).

``estimate`` is honest ``None`` until P1's estimator adapter wires (never a
fabricated ETA). All collection/serialisation is deterministic
(``sort_keys`` + ``allow_nan=False``) so two identical manifests are
byte-identical (Golden Rule #4/#7). Damage on load ⇒ ``None`` ⇒ the caller
rebuilds — a corrupt manifest never crashes a reader.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from brutex.engine.run_layout import RunLayout

#: The manifest on-disk schema version (bump on any breaking shape change).
MANIFEST_SCHEMA_VERSION = 1

#: The canonical serialisation ORDER for ``per_door_kills`` (D-8). Doors present
#: in the mapping serialise in this order; the five hard-gate splits
#: (``non_finite_input`` … ``consecutive_losses``, the ``gate_reasons`` wire
#: vocabulary) hold their slot in the ORDER but stay ABSENT from the DATA until
#: P2 folds the counters (Ruling 12). Any door NOT listed here (a future
#: channel) serialises AFTER these, name-sorted, so nothing is ever dropped.
CANONICAL_DOOR_ORDER: tuple[str, ...] = (
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
    "non_finite_input",
    "min_trades",
    "profit_factor",
    "max_drawdown",
    "win_rate_wlr",
    "consecutive_losses",
)

#: The three dedup-skip attribution channels (D-7 ``dedup_skips_detail``).
DEDUP_DETAIL_KEYS: tuple[str, ...] = ("cache", "resume", "sentinel")

_DOOR_INDEX = {door: i for i, door in enumerate(CANONICAL_DOOR_ORDER)}


class RunManifestError(ValueError):
    """A manifest field violates a binding law (bad schema / negative count / …)."""


def _utc_now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 ``...Z`` string."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _default_dedup_detail() -> dict[str, int]:
    """Return a fresh zero-filled ``dedup_skips_detail`` map."""
    return dict.fromkeys(DEDUP_DETAIL_KEYS, 0)


def _default_preflight() -> dict[str, list[Any]]:
    """Return a fresh empty ``preflight`` map (``{refusals: [], clamps: []}``)."""
    return {"refusals": [], "clamps": []}


def funnel_rows(pairs: Iterable[tuple[str, int]]) -> list[dict[str, Any]]:
    """Return the ordered ``[{stage, count}]`` funnel list for ``actual.funnel``.

    A helper so a caller builds the P4 funnel sub-shape (ordered stage→count)
    consistently rather than hand-rolling it.

    Parameters
    ----------
    pairs
        Ordered ``(stage, count)`` pairs.

    Returns
    -------
    list[dict]
        The ordered funnel rows.
    """
    return [{"stage": stage, "count": count} for stage, count in pairs]


@dataclass(frozen=True, slots=True)
class RunManifest:
    """The per-run forensic manifest — the nine contract keys + additive fields.

    Immutable value object; build once at run-end (typically the orchestrator
    seam folds the live counters in) then :meth:`write` it through the run's
    :class:`~brutex.engine.run_layout.RunLayout`.

    See the module docstring for the D-7 (scalar ``dedup_skips``) and D-8
    (``per_door_kills`` ABSENT-key) laws.
    """

    run_id: str
    selection: Mapping[str, Any] = field(default_factory=dict)
    estimate: Mapping[str, Any] | None = None
    actual: Mapping[str, Any] = field(default_factory=dict)
    per_stage_timings: Mapping[str, float] = field(default_factory=dict)
    per_door_kills: Mapping[str, int] = field(default_factory=dict)
    dedup_skips: int = 0
    tripwires: tuple[Mapping[str, Any], ...] = ()
    suspect: bool = False
    schema_version: int = MANIFEST_SCHEMA_VERSION
    attempt: int = 1
    exit_code: int | None = None
    updated_utc: str = field(default_factory=_utc_now_iso)
    suspect_reasons: tuple[str, ...] = ()
    dedup_skips_detail: Mapping[str, int] = field(default_factory=_default_dedup_detail)
    preflight: Mapping[str, Any] = field(default_factory=_default_preflight)
    recorder_degraded: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise RunManifestError(
                f"schema_version must be {MANIFEST_SCHEMA_VERSION}, got {self.schema_version!r}"
            )
        if not isinstance(self.run_id, str) or not self.run_id:
            raise RunManifestError(f"run_id must be a non-empty str, got {self.run_id!r}")
        # D-7: dedup_skips is a scalar non-negative int (bool is not an int here).
        if isinstance(self.dedup_skips, bool) or not isinstance(self.dedup_skips, int):
            raise RunManifestError(f"dedup_skips must be an int, got {self.dedup_skips!r}")
        if self.dedup_skips < 0:
            raise RunManifestError(f"dedup_skips must be >= 0, got {self.dedup_skips}")
        for door, killed in self.per_door_kills.items():
            if isinstance(killed, bool) or not isinstance(killed, int) or killed < 0:
                raise RunManifestError(
                    f"per_door_kills[{door!r}] must be a non-negative int, got {killed!r}"
                )
        for stage, seconds in self.per_stage_timings.items():
            if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
                raise RunManifestError(
                    f"per_stage_timings[{stage!r}] must be numeric, got {seconds!r}"
                )
            if not math.isfinite(seconds) or seconds < 0:
                raise RunManifestError(
                    f"per_stage_timings[{stage!r}] must be finite and >= 0, got {seconds!r}"
                )
        for key in self.dedup_skips_detail:
            if key not in DEDUP_DETAIL_KEYS:
                raise RunManifestError(
                    f"dedup_skips_detail key must be one of {DEDUP_DETAIL_KEYS}, got {key!r}"
                )
        for key, value in self.dedup_skips_detail.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RunManifestError(
                    f"dedup_skips_detail[{key!r}] must be a non-negative int, got {value!r}"
                )
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise RunManifestError(f"attempt must be an int >= 1, got {self.attempt!r}")
        if self.exit_code is not None and (
            isinstance(self.exit_code, bool) or not isinstance(self.exit_code, int)
        ):
            raise RunManifestError(f"exit_code must be an int or None, got {self.exit_code!r}")
        for tw in self.tripwires:
            if not isinstance(tw, Mapping):
                raise RunManifestError(f"each tripwire must be a mapping, got {tw!r}")

    def _per_door_kills_rows(self) -> list[dict[str, Any]]:
        """Serialise ``per_door_kills`` as the ordered ``[{door, killed}]`` list (D-8).

        Canonical-order doors first (present ones only — ABSENT stays absent),
        then any unlisted door name-sorted, so a future channel is never dropped.
        """
        present = dict(self.per_door_kills)
        ordered = sorted(
            present,
            key=lambda door: (_DOOR_INDEX.get(door, len(CANONICAL_DOOR_ORDER)), door),
        )
        return [{"door": door, "killed": present[door]} for door in ordered]

    def to_payload(self) -> dict[str, Any]:
        """Return the deterministic JSON-serialisable manifest payload.

        ``per_door_kills`` serialises as the ordered ``[{door, killed}]`` list
        (D-8); ``dedup_skips`` stays a scalar with the additive
        ``dedup_skips_detail`` breakdown (D-7).
        """
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "selection": dict(self.selection),
            "estimate": None if self.estimate is None else dict(self.estimate),
            "actual": dict(self.actual),
            "per_stage_timings": {k: float(v) for k, v in self.per_stage_timings.items()},
            "per_door_kills": self._per_door_kills_rows(),
            "dedup_skips": self.dedup_skips,
            "dedup_skips_detail": {
                key: int(self.dedup_skips_detail.get(key, 0)) for key in DEDUP_DETAIL_KEYS
            },
            "tripwires": [dict(tw) for tw in self.tripwires],
            "suspect": self.suspect,
            "suspect_reasons": list(self.suspect_reasons),
            "attempt": self.attempt,
            "exit_code": self.exit_code,
            "updated_utc": self.updated_utc,
            "preflight": dict(self.preflight),
            "recorder_degraded": self.recorder_degraded,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RunManifest:
        """Reconstruct a :class:`RunManifest` from a :meth:`to_payload` dict.

        The ``per_door_kills`` ordered ``[{door, killed}]`` list is folded back to
        a mapping (ABSENT-key law preserved — only present rows become keys).

        Raises
        ------
        RunManifestError
            If the payload is not a mapping or a field violates a binding law.
        """
        if not isinstance(payload, Mapping):
            raise RunManifestError(f"manifest payload must be a mapping, got {type(payload)!r}")
        rows = payload.get("per_door_kills", [])
        per_door: dict[str, int] = {}
        if not isinstance(rows, list):
            raise RunManifestError(f"per_door_kills must serialise as a list, got {rows!r}")
        for row in rows:
            if not isinstance(row, Mapping) or "door" not in row or "killed" not in row:
                raise RunManifestError(f"per_door_kills row must be {{door, killed}}, got {row!r}")
            per_door[str(row["door"])] = row["killed"]
        tripwires = tuple(payload.get("tripwires", ()) or ())
        detail = payload.get("dedup_skips_detail") or {}
        return cls(
            run_id=payload["run_id"],
            selection=payload.get("selection", {}) or {},
            estimate=payload.get("estimate"),
            actual=payload.get("actual", {}) or {},
            per_stage_timings=payload.get("per_stage_timings", {}) or {},
            per_door_kills=per_door,
            dedup_skips=payload.get("dedup_skips", 0),
            tripwires=tripwires,
            suspect=bool(payload.get("suspect", False)),
            schema_version=payload.get("schema_version", MANIFEST_SCHEMA_VERSION),
            attempt=payload.get("attempt", 1),
            exit_code=payload.get("exit_code"),
            updated_utc=payload.get("updated_utc") or _utc_now_iso(),
            suspect_reasons=tuple(payload.get("suspect_reasons", ()) or ()),
            dedup_skips_detail=dict(detail),
            preflight=payload.get("preflight") or _default_preflight(),
            recorder_degraded=bool(payload.get("recorder_degraded", False)),
        )

    def write(self, layout: RunLayout) -> str:
        """Atomically write ``manifest.json`` + its stamp sidecar; return the path str.

        Uses :meth:`RunLayout.write_stamped_json` so the manifest carries a
        content-hash stamp (crash-before-stamp ⇒ treated absent ⇒ rebuild).
        """
        dest = layout.write_stamped_json("manifest.json", self.to_payload(), stage="manifest")
        return str(dest)

    @classmethod
    def load(cls, layout: RunLayout) -> RunManifest | None:
        """Return the run's persisted manifest, or ``None`` on ANY damage.

        Missing file / bad JSON / non-mapping / a stamp that does not verify /
        a law violation all return ``None`` (⇒ rebuild) — a corrupt manifest
        never crashes a reader (the resilient-reader contract).
        """
        path = layout.manifest_path
        if not path.exists():
            return None
        if not layout.verify_stamp(path):
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return cls.from_payload(payload)
        except (RunManifestError, KeyError, TypeError, ValueError):
            return None


__all__ = [
    "CANONICAL_DOOR_ORDER",
    "DEDUP_DETAIL_KEYS",
    "MANIFEST_SCHEMA_VERSION",
    "RunManifest",
    "RunManifestError",
    "funnel_rows",
]
