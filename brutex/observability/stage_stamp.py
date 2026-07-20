"""Content-hash stage stamps — write / validate / discard (R42, G7, D-4).

The MID-CHAIN BREAK LAW's mechanism (R42): every between-stage artifact is
written ATOMICALLY and paired with a sidecar stamp
``<artifact>.stamp.json``::

    {"stage": "...", "run_id": "<hex>", "content_hash": "<sha256-64hex>",
     "produced_at_utc": 1789..., "schema_version": 1, "extra": {...}}

The four contract fields (``stage`` / ``run_id`` / ``content_hash`` /
``produced_at_utc``) are FROZEN (sweep-parallel-integration-contracts.md #2);
``schema_version`` + ``extra`` are additive (a cascade stamp carries
``extra.data_snapshot_hash`` — G7).

Stamp-law soundness (I-5)
-------------------------
Artifact FIRST (the caller's atomic write), stamp SECOND (this module's
atomic write via the COMMON :func:`brutex.data.atomic_io.atomic_write_simple`)
— so a kill between the two leaves an UNSTAMPED artifact, which validation
reports as ``MISSING_STAMP`` → rebuild: always the safe direction.
:func:`validate_stage_stamp` ALWAYS recomputes the full streamed SHA-256 of
the artifact bytes — a torn artifact, a stale stamp from a previous run, or a
swapped stamp can never pass. HONEST envelope: validation is O(artifact
bytes) by design (the whole point is re-hashing the content — bench-pinned as
the honest linear); :func:`read_stage_stamp` (stamp parse only) is flat in
artifact size.

Consumers act on the verdict, never on trust: anything but
:attr:`StampVerdict.OK` means the artifact is NOT consumed —
:func:`discard_and_flag` renames it (plus its stamp) to a forensically-kept
``<name>.discarded.<n>`` (NEVER deleted) and records one ``QUARANTINE``
flight-recorder record, then the producing stage rebuilds (R42's
"auto-discard + rebuild from intact previous stage").
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from brutex.data.atomic_io import atomic_write_simple
from brutex.data.guarded_read import open_regular_binary, read_regular_text
from brutex.models import BruteXError
from brutex.observability import observability_rust
from brutex.observability.flight_recorder import FlightRecorder

__all__ = [
    "SNAPSHOT_EXTRA_KEY",
    "STAGE3_CELL_STAGE",
    "STAMP_SCHEMA_VERSION",
    "STAMP_SUFFIX",
    "StageStamp",
    "StageStampError",
    "StampVerdict",
    "content_hash_of",
    "discard_and_flag",
    "read_stage_stamp",
    "stamp_path",
    "stamp_stage3_cells",
    "validate_stage3_cells",
    "validate_stage_stamp",
    "write_stage_stamp",
]

#: Sidecar suffix appended to the artifact's FULL name (contracts file #2).
STAMP_SUFFIX: Final[str] = ".stamp.json"

#: Stamp schema version (design §3).
STAMP_SCHEMA_VERSION: Final[int] = 1

#: The stage name stamped on every stage-3 CELL store (D-4).
STAGE3_CELL_STAGE: Final[str] = "stage3_cell"

#: The ``extra`` key carrying the run's data snapshot hash (G7 cascade stamps).
SNAPSHOT_EXTRA_KEY: Final[str] = "data_snapshot_hash"

#: Streamed-hash read chunk (1 MiB) — bounded memory on multi-GB artifacts.
_HASH_CHUNK_BYTES: Final[int] = 1 << 20

#: The three string contract fields, in the FROZEN acceptance order the D1
#: shape law indexes (reject indices 1..3 → these names).
_SHAPE_FIELD_NAMES: Final[tuple[str, str, str]] = ("stage", "run_id", "content_hash")

#: The D1 reject index for the last string contract field (indices 1..3).
_SHAPE_REJECT_FIELD_MAX: Final[int] = 3

#: The D1 reject index for a non-number ``produced_at_utc``.
_SHAPE_REJECT_PRODUCED: Final[int] = 4

#: The D1 reject index for a non-str→str ``extra``.
_SHAPE_REJECT_EXTRA: Final[int] = 5


class StageStampError(BruteXError):
    """A stamp could not be written / read, or an artifact could not be hashed."""


class StampVerdict(StrEnum):
    """The validate-before-consume verdict (R42). Only ``OK`` may be consumed."""

    OK = "ok"
    MISSING_ARTIFACT = "missing_artifact"
    MISSING_STAMP = "missing_stamp"
    HASH_MISMATCH = "hash_mismatch"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class StageStamp:
    """One parsed stamp sidecar (contract fields frozen; ``extra`` additive)."""

    stage: str
    run_id: str
    content_hash: str
    produced_at_utc: float
    extra: dict[str, str] = field(default_factory=dict)


def stamp_path(artifact: Path) -> Path:
    """Return *artifact*'s sidecar stamp path — ``<artifact><STAMP_SUFFIX>``.

    Parameters
    ----------
    artifact
        The stamped artifact.

    Returns
    -------
    Path
        The sibling ``<name>.stamp.json`` path.
    """
    return artifact.with_name(artifact.name + STAMP_SUFFIX)


def content_hash_of(artifact: Path) -> str:
    """Streamed SHA-256 of *artifact*'s bytes (full 64-hex, I-5).

    HONEST: O(artifact bytes) — one bounded-chunk read pass.

    Parameters
    ----------
    artifact
        The file to hash.

    Returns
    -------
    str
        The 64-hex SHA-256 digest.

    Raises
    ------
    StageStampError
        When the artifact cannot be read (missing / unreadable / a directory).
    """
    digest = hashlib.sha256()
    try:
        with open_regular_binary(artifact) as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as exc:
        raise StageStampError(f"cannot hash artifact {artifact}: {exc}") from exc
    return digest.hexdigest()


def write_stage_stamp(
    artifact: Path,
    *,
    stage: str,
    run_id: str,
    extra: Mapping[str, str] | None = None,
) -> Path:
    """Stamp *artifact* — artifact FIRST, stamp SECOND (I-5), atomic write.

    Parameters
    ----------
    artifact
        The already-durably-written artifact (MUST exist — the artifact-first
        law; a missing artifact is refused, never stamped in advance).
    stage
        The producing stage name (e.g. ``"cascade_manifest"``).
    run_id
        The run identity the artifact belongs to (D-1).
    extra
        Additive string→string fields (e.g. ``{"data_snapshot_hash": ...}``).

    Returns
    -------
    Path
        The written stamp sidecar path.

    Raises
    ------
    StageStampError
        Missing artifact, a non-string ``extra`` entry, or any write failure.
    """
    if not artifact.is_file():
        raise StageStampError(
            f"artifact {artifact} does not exist — the I-5 law stamps only durably-written artifacts"
        )
    extras: dict[str, str] = {} if extra is None else dict(extra)
    for key, value in extras.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise StageStampError(f"stamp extra entries must be str→str, got {key!r}: {value!r}")
    payload = {
        "stage": stage,
        "run_id": run_id,
        "content_hash": content_hash_of(artifact),
        "produced_at_utc": time.time(),
        "schema_version": STAMP_SCHEMA_VERSION,
        "extra": extras,
    }
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    dest = stamp_path(artifact)
    try:
        atomic_write_simple(dest, lambda tmp: tmp.write_text(rendered, encoding="utf-8"))
    except OSError as exc:
        raise StageStampError(f"failed to write stamp {dest}: {exc}") from exc
    return dest


def _stamp_shape_index_python(
    root_is_dict: bool,
    stage_is_str: bool,
    run_id_is_str: bool,
    hash_is_str: bool,
    produced_ok: bool,
    extra_ok: bool,
) -> int:
    """The strict-parse acceptance-ORDER law (D1) — the LIVE parity oracle.

    First-false wins, in the FROZEN order of the pre-seam ``_parse_stamp``
    raise chain (DEC-RUST-OBSERVABILITY-001). Index vocabulary: ``-1``
    accept · ``0`` root-not-dict · ``1`` stage · ``2`` run_id ·
    ``3`` content_hash · ``4`` produced_at_utc · ``5`` extra.
    """
    checks = (root_is_dict, stage_is_str, run_id_is_str, hash_is_str, produced_ok, extra_ok)
    for index, ok in enumerate(checks):
        if not ok:
            return index
    return -1


def _parse_stamp(text: str, *, source: Path) -> StageStamp:
    """Parse stamp JSON *text* strictly — :class:`StageStampError` on any defect.

    ``json.loads`` stays Python (the CPython-parse twin is the named
    follow-up ``DEC-RUST-OBSERVABILITY-STAMP-PARSE-003``); the post-parse shape checks
    are probed into bools (side-effect-free ``isinstance`` + ``dict.get``,
    hoisted unconditionally above the raises — behavior-identical) and the
    acceptance ORDER routes through the default-OFF ``observability_rust``
    facade (law D1) with :func:`_stamp_shape_index_python` as the oracle;
    this function materializes the EXACT pre-seam error message for the
    returned reject index.
    """
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StageStampError(f"malformed stamp JSON at {source}: {exc}") from exc
    root_is_dict = isinstance(raw, dict)
    if root_is_dict:
        stage_is_str = isinstance(raw.get("stage"), str)
        run_id_is_str = isinstance(raw.get("run_id"), str)
        hash_is_str = isinstance(raw.get("content_hash"), str)
        produced = raw.get("produced_at_utc")
        produced_ok = isinstance(produced, int | float) and not isinstance(produced, bool)
        extra_raw = raw.get("extra", {})
        extra_ok = isinstance(extra_raw, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in extra_raw.items()
        )
    else:
        # A non-dict root cannot be probed further (`raw.get` may not even
        # exist); the shape law rejects at index 0 regardless, so the
        # remaining probes are vacuous constants — identical on both arms,
        # never consulted by the first-false-wins ordering.
        stage_is_str = run_id_is_str = hash_is_str = produced_ok = extra_ok = True
    reject = observability_rust.routed_stamp_shape_index(
        root_is_dict,
        stage_is_str,
        run_id_is_str,
        hash_is_str,
        produced_ok,
        extra_ok,
        python_law=_stamp_shape_index_python,
    )
    if reject == 0:
        raise StageStampError(f"stamp root at {source} must be an object, got {type(raw).__name__}")
    if 1 <= reject <= _SHAPE_REJECT_FIELD_MAX:
        key = _SHAPE_FIELD_NAMES[reject - 1]
        raise StageStampError(f"stamp {source} field {key!r} must be a str, got {raw.get(key)!r}")
    if reject == _SHAPE_REJECT_PRODUCED:
        raise StageStampError(f"stamp {source} field 'produced_at_utc' must be a number")
    if reject == _SHAPE_REJECT_EXTRA:
        raise StageStampError(f"stamp {source} field 'extra' must be a str→str object")
    return StageStamp(
        stage=raw["stage"],
        run_id=raw["run_id"],
        content_hash=raw["content_hash"],
        produced_at_utc=float(raw["produced_at_utc"]),
        extra=dict(raw.get("extra", {})),
    )


def read_stage_stamp(artifact: Path) -> StageStamp:
    """Read + strictly parse *artifact*'s stamp sidecar (flat in artifact size).

    Parameters
    ----------
    artifact
        The stamped artifact (the SIDECAR is read, not the artifact).

    Returns
    -------
    StageStamp
        The parsed stamp.

    Raises
    ------
    StageStampError
        Missing / unreadable / malformed stamp.
    """
    sidecar = stamp_path(artifact)
    try:
        text = read_regular_text(sidecar)
    except OSError as exc:
        raise StageStampError(f"cannot read stamp {sidecar}: {exc}") from exc
    return _parse_stamp(text, source=sidecar)


def _stamp_verdict_python(
    recomputed: str,
    content_hash: str,
    stage: str,
    expected_stage: str,
    run_id: str,
    expected_run_id: str | None,
    stamped_snapshot: str | None,
    expected_snapshot_hash: str | None,
) -> str:
    """The ``validate_stage_stamp`` mismatch matrix (D2) — the LIVE parity oracle.

    The identical boolean expression, scalarized (``stamp.<field>``
    attribute reads became parameters — DEC-RUST-OBSERVABILITY-001, a
    disclosed verbatim move). Returns the :class:`StampVerdict` VALUE token;
    the caller mints the enum member from it.
    """
    mismatch = (
        recomputed != content_hash
        or stage != expected_stage
        or (expected_run_id is not None and run_id != expected_run_id)
        or (
            expected_snapshot_hash is not None
            and stamped_snapshot is not None
            and stamped_snapshot != expected_snapshot_hash
        )
    )
    if mismatch:
        return "hash_mismatch"
    return "ok"


def validate_stage_stamp(
    artifact: Path,
    *,
    expected_stage: str,
    expected_run_id: str | None = None,
    expected_snapshot_hash: str | None = None,
) -> tuple[StampVerdict, StageStamp | None]:
    """Validate *artifact* against its stamp — ALWAYS recomputes the full hash (I-5).

    Verdict matrix (only ``OK`` may be consumed):

    * artifact missing → ``MISSING_ARTIFACT``;
    * stamp sidecar missing (a kill between artifact and stamp) →
      ``MISSING_STAMP``;
    * stamp unparseable / wrong-shaped → ``MALFORMED``;
    * ANY mismatch — recomputed content hash (torn/corrupt artifact), stage
      (swapped stamp), run id (stale stamp from a previous run), or the
      ``data_snapshot_hash`` extra when BOTH sides assert one (G7 changed-lake
      refusal) → ``HASH_MISMATCH`` with the parsed stamp returned for
      forensics.

    The snapshot check has teeth only when the stamp CARRIES the
    ``data_snapshot_hash`` extra; a stamp without it (e.g. a stage-3 cell
    stamped without one) is not failed on that axis — the run-id check already
    binds the data identity there, since ``run_id = grid_identity`` folds the
    data snapshot hashes (D-1). Absent evidence is never fabricated.

    Parameters
    ----------
    artifact
        The artifact to validate.
    expected_stage
        The stage this consumer expects to read.
    expected_run_id
        When given, the stamp's ``run_id`` must equal it.
    expected_snapshot_hash
        When given AND the stamp carries ``extra.data_snapshot_hash``, the
        two must be equal.

    Returns
    -------
    tuple[StampVerdict, StageStamp | None]
        The verdict plus the parsed stamp where one was readable.

    Raises
    ------
    StageStampError
        Only when the ARTIFACT exists but cannot be read/hashed (a loud
        infrastructure defect, not a stamp verdict).
    """
    if not artifact.is_file():
        return (StampVerdict.MISSING_ARTIFACT, None)
    sidecar = stamp_path(artifact)
    if not sidecar.is_file():
        return (StampVerdict.MISSING_STAMP, None)
    try:
        stamp = _parse_stamp(read_regular_text(sidecar), source=sidecar)
    except (StageStampError, OSError):
        return (StampVerdict.MALFORMED, None)
    # The always-recompute call goes through the MODULE GLOBAL (I-5 — the
    # test_r42_always_recomputes_hash spy monkeypatches it by name).
    recomputed = content_hash_of(artifact)
    stamped_snapshot = stamp.extra.get(SNAPSHOT_EXTRA_KEY)
    # The mismatch matrix routes through the default-OFF observability_rust
    # facade (law D2) with `_stamp_verdict_python` as the live oracle
    # (DEC-RUST-OBSERVABILITY-001; branch-free here). `StampVerdict(token)`
    # is an Enum VALUE-call — it returns the member SINGLETON, so consumers'
    # `verdict is StampVerdict.OK` identity checks hold bit-for-bit.
    token = observability_rust.routed_stamp_verdict(
        recomputed,
        stamp.content_hash,
        stamp.stage,
        expected_stage,
        stamp.run_id,
        expected_run_id,
        stamped_snapshot,
        expected_snapshot_hash,
        python_law=_stamp_verdict_python,
    )
    return (StampVerdict(token), stamp)


def stamp_stage3_cells(
    cell_paths: Sequence[Path],
    *,
    run_id: str,
    snapshot_hash: str | None = None,
) -> int:
    """Stamp every stage-3 CELL store with ``stage="stage3_cell"`` (D-4).

    Parameters
    ----------
    cell_paths
        The cell store paths, each already durably written.
    run_id
        The run identity to bind into every cell stamp.
    snapshot_hash
        Optional data snapshot hash to carry as the
        ``data_snapshot_hash`` extra (additive; ``None`` = the run-id
        binding alone, which already folds the snapshot per D-1).

    Returns
    -------
    int
        The number of cells stamped.

    Raises
    ------
    StageStampError
        On the first cell that cannot be stamped (missing / write failure).
    """
    extra = None if snapshot_hash is None else {SNAPSHOT_EXTRA_KEY: snapshot_hash}
    stamped = 0
    for cell in cell_paths:
        write_stage_stamp(cell, stage=STAGE3_CELL_STAGE, run_id=run_id, extra=extra)
        stamped += 1
    return stamped


def validate_stage3_cells(
    cell_paths: Sequence[Path],
    *,
    run_id: str,
    snapshot_hash: str,
) -> tuple[Path, ...]:
    """Validate every stage-3 cell BEFORE the cascade consumes them (D-4).

    Parameters
    ----------
    cell_paths
        The cell store paths the cascade is about to read.
    run_id
        The expected run identity.
    snapshot_hash
        The expected data snapshot hash (checked where a cell stamp carries
        the extra — see :func:`validate_stage_stamp`).

    Returns
    -------
    tuple[Path, ...]
        The INVALID cells in input order — EMPTY means all cells are
        consumable.
    """
    invalid: list[Path] = []
    for cell in cell_paths:
        verdict, _stamp = validate_stage_stamp(
            cell,
            expected_stage=STAGE3_CELL_STAGE,
            expected_run_id=run_id,
            expected_snapshot_hash=snapshot_hash,
        )
        if verdict is not StampVerdict.OK:
            invalid.append(cell)
    return tuple(invalid)


def discard_and_flag(
    artifact: Path,
    *,
    reason: str,
    recorder: FlightRecorder | None,
) -> None:
    """Discard a failed artifact+stamp pair forensically and flag it (R42).

    Renames *artifact* and its stamp sidecar to ``<name>.discarded.<n>``
    (the smallest ``n`` free for BOTH — a monotonic ordinal, never a
    wall-clock dirname; NEVER deletes) and records ONE ``QUARANTINE``
    flight-recorder record when a *recorder* is given (the recorder itself is
    fail-soft, I-3). A side missing on disk is simply skipped — the flag
    still lands.

    Parameters
    ----------
    artifact
        The artifact that failed validation.
    reason
        The human-readable discard reason (lands in the QUARANTINE record).
    recorder
        The run's flight recorder, or ``None`` (no record, still discarded).

    Raises
    ------
    StageStampError
        When a rename fails (a loud infrastructure defect).
    """
    sidecar = stamp_path(artifact)
    n = 0
    while (
        artifact.with_name(f"{artifact.name}.discarded.{n}").exists()
        or sidecar.with_name(f"{sidecar.name}.discarded.{n}").exists()
    ):
        n += 1
    moved: list[str] = []
    for src in (artifact, sidecar):
        if not src.exists():
            continue
        dest = src.with_name(f"{src.name}.discarded.{n}")
        try:
            src.rename(dest)
        except OSError as exc:
            raise StageStampError(f"failed to discard {src} -> {dest}: {exc}") from exc
        moved.append(dest.name)
    if recorder is not None:
        recorder.record(
            "QUARANTINE",
            {
                "what": "stage_artifact",
                "path_rel": artifact.name,
                "reason": reason,
                "discard_ordinal": n,
                "moved": moved,
            },
        )
