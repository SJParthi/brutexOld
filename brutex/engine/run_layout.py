"""Sweep RUN-CORE layout — the ONE owner of the per-run artifact directory.

DEC-SWEEP-UNKILLABLE-CORE-001 (MIGRATION.md #1605). Closes gap #4 of the
2026-07-08 one-click-sweep gap register (``sweep-gap-register-jul8``): before
this leaf existed there was **no** run-dir helper, no run-level ``run_id``
resolution, and no atomic-write convention — so the run-manifest, the top100
artifact and the (future) flight recorder would each have invented divergent
layouts, breaking the one-run_id threading (R43), and — worse — a Parquet
placed under the ``**/*.parquet``-globbed sweeps tree would crash three
existing readers (``/results`` discovery, the sweep-verify doctor, and
``results_report``) with an unguarded ``KeyError`` → a 500.

This module is the single source of truth (CLAUDE.md §11 COMMON) for:

* **The run root** — every run artifact lives under a **SIBLING** root
  ``<results_root>.parent / sweep_runs / <run_id>/`` (:data:`SWEEP_RUNS_DIRNAME`),
  NEVER inside the ``.parquet``-globbed sweeps tree (Ruling 4). ``results_root``
  is itself ``<lake>/sweeps`` in production, so the sibling is ``<lake>/sweep_runs``
  — structurally invisible to the sweeps-tree readers, so no reader exclusion is
  needed (this module writes only JSON sidecars, never Parquet).
* **The run-level ``run_id``** (:func:`run_level_run_id`) — TODAY the engine grid
  identity (``grid_identity(grid)``); Ruling 9's one-LOGICAL-run_id-wrapping-N
  -engine-runs lands by rebinding this ONE function, callers never mint their own.
* **Atomic artifact writes** (:meth:`RunLayout.write_json` /
  :meth:`RunLayout.write_stamped_json`) — tmp → ``fsync`` → ``os.replace`` via the
  COMMON :func:`brutex.data.atomic_io.atomic_write_simple`, so a kill mid-write
  leaves either the old file or the new one, never a partial.
* **Stage-stamp sidecars** (:meth:`RunLayout.write_stamped_json`) — every stamped
  artifact gets a sibling ``<artifact>.stamp.json`` = ``{stage, run_id,
  content_hash, produced_at_utc, schema_version, extra}`` (:data:`STAMP_SCHEMA_VERSION`);
  the ``content_hash`` is the full streamed ``sha256`` of the artifact bytes,
  which a consumer ALWAYS recomputes before trusting the file (the P2→P3 stage-stamp
  contract). The stamp is written LAST (a crash ⇒ a stamp-less artifact ⇒ treated
  absent ⇒ rebuild).

Honest envelope (Golden Rule #10): this leaf owns *layout mechanics only* — it
imports no engine module (only the COMMON atomic writer + stdlib) so it cannot
introduce an import cycle and is trivially unit-testable to 100% line+branch.
Path validation is a strict allowlist (:data:`_RUN_ID_RE`) so a hostile /
malformed ``run_id`` can never escape the sibling root via ``..`` or a path
separator.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from brutex.data.atomic_io import atomic_write_simple

#: The SIBLING directory name beside the sweeps tree (Ruling 4). Run artifacts
#: live at ``<results_root>.parent / sweep_runs / <run_id>/`` so a foreign file
#: under it is never reached by the ``.parquet``-globbed sweeps-tree readers.
SWEEP_RUNS_DIRNAME = "sweep_runs"

#: Stage-stamp / artifact schema version (bump on any breaking on-disk change).
STAMP_SCHEMA_VERSION = 1

#: A run-level ``run_id`` must be a path-safe token: a lowercase/uppercase
#: alphanumeric head then ``[0-9A-Za-z_-]`` (NO ``.`` — so ``..`` traversal is
#: structurally impossible, NO ``/`` or ``\`` — no separator escape), 8–128 chars.
#: The engine grid identity (a 64-char BLAKE3 hex) satisfies it, and a future
#: Ruling-9 logical id such as ``multi-<hex>`` fits too.
_RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,127}$")


class RunLayoutError(ValueError):
    """A malformed run-level ``run_id`` (not a path-safe token)."""


def validate_run_id(run_id: str) -> str:
    """Return *run_id* unchanged iff it is a path-safe run-level token.

    Parameters
    ----------
    run_id
        The candidate run-level identity.

    Returns
    -------
    str
        The validated ``run_id``.

    Raises
    ------
    RunLayoutError
        If ``run_id`` is not a :data:`_RUN_ID_RE` match (empty, too short/long,
        or containing ``.`` / a path separator / any other unsafe character) — so
        it can never escape the sibling run root.
    """
    if not _RUN_ID_RE.match(run_id):
        raise RunLayoutError(
            f"run_id must be a path-safe token matching {_RUN_ID_RE.pattern!r}, got {run_id!r}"
        )
    return run_id


def run_level_run_id(grid_identity_hex: str) -> str:
    """Return the run-level ``run_id`` for a grid — the ONE resolution seam.

    TODAY the engine grid identity itself (``grid_identity(grid)``, a 64-char
    BLAKE3 hex). Ruling 9 (one logical run_id wrapping N engine runs) and Ruling
    17 (the run-level-vs-combo-level ``run_id`` disambiguation) both land by
    rebinding THIS function — every artifact writer resolves its run_id here, so
    callers never mint their own layout (gap #4 / R43 one-run_id threading).

    Parameters
    ----------
    grid_identity_hex
        The engine grid identity hex (``brutex.engine.sweep_supervisor.grid_identity``).

    Returns
    -------
    str
        The validated run-level ``run_id``.

    Raises
    ------
    RunLayoutError
        If the identity is not a path-safe token.
    """
    return validate_run_id(grid_identity_hex)


def sweep_runs_root(results_root: Path) -> Path:
    """Return the SIBLING run root beside the sweeps tree.

    ``<results_root>.parent / sweep_runs`` — a sibling of the ``.parquet``-globbed
    sweeps tree (in production ``results_root`` is ``<lake>/sweeps`` so this is
    ``<lake>/sweep_runs``). Deliberately OUTSIDE the sweeps tree so ``/results``
    discovery, the sweep-verify doctor, and ``results_report`` never glob a run
    artifact (Ruling 4).

    Parameters
    ----------
    results_root
        The sweep results root (the ``.parquet``-globbed tree).

    Returns
    -------
    pathlib.Path
        The sibling ``sweep_runs`` root (not created).
    """
    return results_root.parent / SWEEP_RUNS_DIRNAME


def run_dir(results_root: Path, run_id: str) -> Path:
    """Return the per-run directory ``<sweep_runs>/<run_id>/`` (not created).

    Parameters
    ----------
    results_root
        The sweep results root.
    run_id
        The validated run-level identity.

    Returns
    -------
    pathlib.Path
        The run directory.

    Raises
    ------
    RunLayoutError
        If ``run_id`` is not a path-safe token.
    """
    return sweep_runs_root(results_root) / validate_run_id(run_id)


def _utc_now_iso() -> str:
    """Return the current UTC instant as an ISO-8601 ``...Z`` string."""
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _dumps(payload: Any) -> bytes:
    """Serialize *payload* to deterministic UTF-8 JSON bytes (Golden Rule #4/#7).

    Sorted keys, compact separators, ``allow_nan=False`` (a NaN/Inf in an
    artifact is a fabrication bug, not silently emitted as invalid JSON).
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


@dataclass(frozen=True, slots=True)
class RunLayout:
    """The resolved artifact layout for ONE sweep run — path mechanics + atomic writes.

    Every run-end artifact (manifest, exit-status, chain-map, flight recorder,
    top100, stamps) resolves its path THROUGH one :class:`RunLayout` so the
    layout is single-sourced (gap #4). Immutable — reused freely.

    Attributes
    ----------
    results_root
        The sweep results root (the ``.parquet``-globbed sweeps tree).
    run_id
        The validated run-level identity (:func:`run_level_run_id`).
    """

    results_root: Path
    run_id: str

    def __post_init__(self) -> None:
        validate_run_id(self.run_id)

    @property
    def run_dir(self) -> Path:
        """The per-run directory ``<sweep_runs>/<run_id>/`` (not created)."""
        return run_dir(self.results_root, self.run_id)

    def artifact_path(self, name: str) -> Path:
        """Return ``<run_dir>/<name>`` for a run artifact.

        Parameters
        ----------
        name
            The bare artifact filename (must not contain a path separator).

        Raises
        ------
        RunLayoutError
            If ``name`` is empty, contains a path separator, or is ``.``/``..``.
        """
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise RunLayoutError(f"artifact name must be a bare filename, got {name!r}")
        return self.run_dir / name

    @property
    def manifest_path(self) -> Path:
        """The run manifest path ``<run_dir>/manifest.json``."""
        return self.artifact_path("manifest.json")

    @property
    def exit_status_path(self) -> Path:
        """The exit-status path ``<run_dir>/exit-status.json`` (writer is a follow-up)."""
        return self.artifact_path("exit-status.json")

    @property
    def chain_map_path(self) -> Path:
        """The chain-map path ``<run_dir>/chain-map.json`` (writer is a follow-up)."""
        return self.artifact_path("chain-map.json")

    @property
    def recorder_path(self) -> Path:
        """The flight-recorder path ``<run_dir>/flight-recorder.jsonl`` (writer is a follow-up)."""
        return self.artifact_path("flight-recorder.jsonl")

    @staticmethod
    def stamp_path(artifact: Path) -> Path:
        """Return the stage-stamp sidecar path ``<artifact>.stamp.json`` for *artifact*."""
        return artifact.with_name(artifact.name + ".stamp.json")

    def ensure_dir(self) -> Path:
        """Create :attr:`run_dir` (parents included) and return it — idempotent."""
        target = self.run_dir
        target.mkdir(parents=True, exist_ok=True)
        return target

    def write_json(self, name: str, payload: Any) -> Path:
        """Atomically write *payload* as deterministic JSON to ``<run_dir>/<name>``.

        Creates the run dir if absent. Returns the written path.

        Parameters
        ----------
        name
            The bare artifact filename.
        payload
            Any JSON-serialisable object (``allow_nan=False``).

        Returns
        -------
        pathlib.Path
            The written artifact path.
        """
        self.ensure_dir()
        dest = self.artifact_path(name)
        content = _dumps(payload)
        atomic_write_simple(dest, lambda tmp: tmp.write_bytes(content))
        return dest

    def write_stamped_json(
        self, name: str, payload: Any, *, stage: str, extra: dict[str, Any] | None = None
    ) -> Path:
        """Atomically write a stamped artifact + its ``<artifact>.stamp.json`` sidecar.

        The artifact is written FIRST, then the stamp sidecar carrying the full
        streamed ``sha256`` of the artifact's bytes — so a crash after the artifact
        but before the stamp yields a stamp-less artifact (treated absent ⇒
        rebuild), and a consumer ALWAYS recomputes the hash before trusting the
        file (the P2→P3 stage-stamp contract).

        Parameters
        ----------
        name
            The bare artifact filename.
        payload
            The artifact payload (JSON-serialisable, ``allow_nan=False``).
        stage
            The producing stage token stamped into the sidecar (e.g. ``"manifest"``).
        extra
            Optional extra provenance folded into the stamp's ``extra`` object
            (e.g. ``data_snapshot_hash`` on a cascade stamp); ``None`` ⇒ ``{}``.

        Returns
        -------
        pathlib.Path
            The written ARTIFACT path (the stamp is at :meth:`stamp_path`).
        """
        dest = self.write_json(name, payload)
        content_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        stamp = {
            "stage": stage,
            "run_id": self.run_id,
            "content_hash": content_hash,
            "produced_at_utc": _utc_now_iso(),
            "schema_version": STAMP_SCHEMA_VERSION,
            "extra": dict(extra) if extra else {},
        }
        stamp_dest = self.stamp_path(dest)
        stamp_content = _dumps(stamp)
        atomic_write_simple(stamp_dest, lambda tmp: tmp.write_bytes(stamp_content))
        return dest

    def verify_stamp(self, artifact: Path) -> bool:
        """Return ``True`` iff *artifact* has a well-formed stamp matching its CURRENT bytes.

        The stage-stamp verify tier: the sidecar must exist, be valid JSON of the
        current schema, name this ``run_id``, and its ``content_hash`` must equal
        the full streamed ``sha256`` of the artifact's bytes as they are NOW. Any
        mismatch / missing / damaged input ⇒ ``False`` (⇒ treat absent ⇒ rebuild),
        never a raise.

        Parameters
        ----------
        artifact
            The stamped artifact path.

        Returns
        -------
        bool
            Whether the stamp is present, well-formed, and current.
        """
        stamp_dest = self.stamp_path(artifact)
        if not artifact.exists() or not stamp_dest.exists():
            return False
        try:
            stamp = json.loads(stamp_dest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if not isinstance(stamp, dict):
            return False
        if stamp.get("schema_version") != STAMP_SCHEMA_VERSION:
            return False
        if stamp.get("run_id") != self.run_id:
            return False
        recorded = stamp.get("content_hash")
        if not isinstance(recorded, str):
            return False
        try:
            current = hashlib.sha256(artifact.read_bytes()).hexdigest()
        except OSError:
            return False
        return current == recorded


__all__ = [
    "STAMP_SCHEMA_VERSION",
    "SWEEP_RUNS_DIRNAME",
    "RunLayout",
    "RunLayoutError",
    "run_dir",
    "run_level_run_id",
    "sweep_runs_root",
    "validate_run_id",
]
