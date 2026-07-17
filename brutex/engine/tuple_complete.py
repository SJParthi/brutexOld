"""Per-tuple completion sentinel — the redo authority (DEC-MARK-MANY-SENTINEL-001).

Closes the partial-``mark_many`` redo hole tracked at MIGRATION.md #650
(ADR-0008 condition 10): ``run_persisted_sweep`` marks a tuple's combo
identities one key at a time, so a crash inside the mark loop leaves a marked
SUBSET; a redo that trusts those marks re-ranks the unmarked remainder —
a wedge under ``--overfit-gate`` (subset deflation ⇒ store append conflict)
and silent extra rows under ``top_k`` truncation (subset top-K ⇒ tree
byte-identity broken).

The fix (per the 2026-06-11 hostile review recorded in MIGRATION.md #651):
**the sentinel is authoritative, the dedup marks are advisory.** Each tuple
run with a digest writes ``<store>.parquet.complete.json`` STRICTLY LAST —
after the store flush, after the trial-stats sidecar, after ``mark_many``.
On re-entry the sweep consults ONLY the sentinel:

* sentinel present + fully paired (tuple digest, store content hash, sidecar
  pairing) → the tuple is COMPLETE: short-circuit, touch nothing;
* sentinel absent or unpaired → the tuple is INCOMPLETE: re-run with dedup
  probes BYPASSED (the full mask population, never a subset), then rewrite
  every artifact byte-identically and re-mark — self-healing, idempotent
  (Golden Rule #7).

No unmark API is needed: stale marks are never consulted on the redo path, so
the Bloom tier's inability to delete (and a Bloom false-positive after any
future exact-tier delete) is irrelevant by construction.

Honest envelope (Golden Rule #10)
---------------------------------
* The sentinel proves the WRITE SEQUENCE completed; it does not heal a
  pre-fix tree where the ``top_k`` inflation already fired and the tuple then
  completed — detection/recount for those trees is the tracked follow-up in
  MIGRATION.md #651 (the ADR-0008 §3-D deterministic recount), not this module.
* Per-tuple cost: one small JSON read + one SHA-256 over the (≤ ``top_k``-row)
  store Parquet on resume, one small atomic write on completion. The
  per-combo hot path is untouched. No O(1)-total claim.
* The dedup tiers stay exactly as they are (volatile in production); they are
  an optimization for legacy no-digest callers, never load-bearing for redo
  correctness on the digest path.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.engine.results_store import _atomic_write
from brutex.engine.sweep_lookahead import env_flag_tristate
from brutex.engine.trial_stats import TrialStatsError, _validate_hex, trial_stats_sidecar_path
from brutex.models import BruteXError

#: Sentinel schema version — bump on any incompatible field change.
COMPLETION_SCHEMA_VERSION: Final[int] = 1

#: Sentinel filename suffix, appended to the tuple store's full name
#: (``bull_k3_<digest>.parquet`` → ``bull_k3_<digest>.parquet.complete.json``).
COMPLETION_SUFFIX: Final[str] = ".complete.json"

#: The tuple path digest length (hex chars) — the store filename's collision
#: guard; matches the trial-stats sidecar's pairing identity.
_TUPLE_DIGEST_LEN: Final[int] = 16

#: SHA-256 hex length — the store's ``FlushResult.content_hash`` format.
_CONTENT_HASH_LEN: Final[int] = 64

#: The exact payload keys a v1 sentinel must carry (sorted; the JSON contract).
_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "n_marked",
    "schema_version",
    "sidecar_written",
    "store_content_hash",
    "tuple_digest",
)

#: Kill switch for the resume store guard (DEC-SWEEP-RESUME-STORE-GUARD-001).
#: DEFAULT ON — a data-loss guard must never require opt-in; the explicit OFF
#: restores the byte-identical pre-guard skip loop (the honest escape hatch for
#: an emergency revert or a pathological network-FS stat-latency environment —
#: with the switch off the S5b silent-loss mode RETURNS; stated, not papered
#: over).
RESUME_STORE_GUARD_ENV: Final[str] = "BRUTEX_SWEEP_RESUME_STORE_GUARD"

#: The artifact names :func:`missing_tuple_artifacts` reports, in sorted order.
TUPLE_ARTIFACT_NAMES: Final[tuple[str, ...]] = ("sentinel", "sidecar", "store")


def resume_store_guard_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Resolve the :data:`RESUME_STORE_GUARD_ENV` kill switch — fail-loud on junk.

    unset / ``""`` → ``True`` (default-ON: the guard closes a silent
    permanent-data-loss mode — the S5b operator scenario "massive pull →
    delete → re-run must never silently lose data" — so opt-in would ship the
    loss mode by default); truthy/falsy tokens per the ONE sweep env
    vocabulary (:func:`brutex.engine.sweep_lookahead.env_flag_tristate` —
    imported, never copied); anything else raises (the fail-loud env
    convention — a typo must neither silently disarm a data-loss guard nor
    silently arm anything). The fail-safe direction is deliberately INVERTED
    versus ``rust_metrics``' warn-once-OFF: there OFF is the safe oracle arm,
    here ON is the safe arm.

    Parameters
    ----------
    env
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the value is set to something outside the two token sets.
    """
    mapping = os.environ if env is None else env
    raw = mapping.get(RESUME_STORE_GUARD_ENV, "")
    if not raw.strip():
        return True
    flag = env_flag_tristate(raw)
    if flag is None:
        raise ValueError(
            f"{RESUME_STORE_GUARD_ENV}={raw!r} is not a recognised switch value; expected "
            "one of '1', 'on', 'true', 'yes' (enable — the default when unset) or "
            "'0', 'off', 'false', 'no' (disable — restores the pre-guard skip loop, "
            "re-opening the S5b deleted-store silent-loss mode)"
        )
    return flag


def missing_tuple_artifacts(store_path: Path) -> tuple[str, ...]:
    """Return the sorted names of the tuple's MISSING persisted artifacts — O(1).

    The INTACT-ARTIFACTS PREDICATE of the resume store guard
    (DEC-SWEEP-RESUME-STORE-GUARD-001): exactly THREE stat-level existence
    probes, zero reads, zero hashing —

    1. ``store`` — the tuple's Parquet at
       :func:`~brutex.engine.sweep_orchestrator.tuple_store_path` (present even
       for a completed ZERO-finalist tuple: ``ResultsStore.flush`` always
       renders the full schema from possibly-empty row lists, so a legal
       completed-empty tuple persists a schema-only 0-row Parquet);
    2. ``sentinel`` — the paired completion sentinel
       (:func:`completion_sentinel_path`);
    3. ``sidecar`` — the trial-stats sidecar
       (:func:`~brutex.engine.trial_stats.trial_stats_sidecar_path`). On this
       tree the sentinel and the sidecar are written under the SAME
       ``trial_stats_digest is not None`` condition and the grid drivers always
       supply the digest, so sentinel-present ⟹ sidecar-was-written — a missing
       sidecar is therefore always a DELETION (the same silent-loss class,
       feeding Stage-4 DSR deflation), and the stat is free.

    ``()`` = INTACT (a resume/cache skip is allowed); any missing name → the
    caller falls through to ``run_one_tuple``, whose existing
    ``_sentinel_resume`` self-heals with a FULL byte-identical redo.

    WHAT COUNTS AS PAIRED — filename-level only: both sibling names are derived
    by appending suffixes to the full store filename, which embeds the 16-hex
    BLAKE3 tuple digest (the collision guard), so name-adjacency IS the pairing
    at stat level. The sentinel's ``tuple_digest`` FIELD and the
    ``store_content_hash`` are deliberately NOT read or re-hashed here — a JSON
    read + SHA-256 over the store per skipped tuple would violate the
    O(1)-stat contract.

    HONEST ENVELOPE (Golden Rule #10) — what this deliberately does NOT
    detect: CORRUPTION. A present-but-truncated/tampered/zero-byte Parquet, a
    corrupt or digest-mismatched sentinel body, and a corrupt sidecar all read
    INTACT at this gate. Content verification (sentinel read + tuple-digest
    match + SHA-256 store re-hash + sidecar pairing) remains
    ``_sentinel_resume``'s job when the tuple actually re-enters, and the G1
    ``store_quarantine`` seam's job on a run-path open failure. This gate
    closes exactly the MISSING-artifact class — the S5b operator scenario.
    """
    missing: list[str] = []
    if not completion_sentinel_path(store_path).is_file():
        missing.append("sentinel")
    if not trial_stats_sidecar_path(store_path).is_file():
        missing.append("sidecar")
    if not store_path.is_file():
        missing.append("store")
    return tuple(missing)


class TupleCompletionError(BruteXError):
    """Raised for any completion-sentinel failure.

    Covers an invalid field (negative mark count, malformed pairing identity,
    a non-boolean ``sidecar_written``), a missing or malformed sentinel file,
    and an unsupported schema version.
    """


def _validate_hex_field(name: str, value: str, length: int) -> None:
    """Validate via the ONE hex validator (CLAUDE.md §11 COMMON), re-typed.

    Delegates to :func:`brutex.engine.trial_stats._validate_hex` (the pairing
    identities here are the SAME formats the trial-stats sidecar validates)
    and re-raises as :class:`TupleCompletionError` — the
    ``LeaderboardTrialsRecord`` construct-to-validate precedent.
    """
    try:
        _validate_hex(name, value, length)
    except TrialStatsError as exc:
        raise TupleCompletionError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class TupleCompletionSentinel:
    """One tuple's completion proof — the sentinel's in-memory form.

    Parameters
    ----------
    tuple_digest
        The tuple's 16-hex path digest (the store filename's collision guard)
        — pairing identity half one (the trial-stats sidecar discipline,
        ADR-0008 condition 5).
    store_content_hash
        SHA-256 hex of the store file this sentinel completes
        (``FlushResult.content_hash``) — pairing identity half two. Re-hashed
        from the actual store bytes on resume, so a tampered or swapped store
        invalidates the sentinel rather than being falsely skipped.
    sidecar_written
        Whether this run wrote the trial-stats sidecar — when ``True``, the
        resume check additionally verifies the sidecar still pairs.
    n_marked
        How many identities ``mark_many`` processed before the sentinel was
        written (audit context; the marks themselves are advisory).

    Raises
    ------
    TupleCompletionError
        On a negative ``n_marked``, a malformed pairing identity, or a
        non-boolean ``sidecar_written``.
    """

    tuple_digest: str
    store_content_hash: str
    sidecar_written: bool
    n_marked: int

    def __post_init__(self) -> None:
        """Validate every field invariant at construction time."""
        if self.n_marked < 0:
            raise TupleCompletionError(f"n_marked must be >= 0, got {self.n_marked}")
        if not isinstance(self.sidecar_written, bool):
            raise TupleCompletionError(
                f"sidecar_written must be a bool, got {self.sidecar_written!r}"
            )
        _validate_hex_field("tuple_digest", self.tuple_digest, _TUPLE_DIGEST_LEN)
        _validate_hex_field("store_content_hash", self.store_content_hash, _CONTENT_HASH_LEN)


def completion_sentinel_path(store_path: Path) -> Path:
    """Return the sentinel path for a tuple store — ``<store>.complete.json``.

    The suffix APPENDS to the full store filename (digest included), so the
    sentinel inherits the store path's collision guarantees and the pairing is
    visible in a directory listing — the trial-stats sidecar convention.
    """
    return store_path.with_name(store_path.name + COMPLETION_SUFFIX)


def render_completion_sentinel(sentinel: TupleCompletionSentinel) -> bytes:
    """Serialize *sentinel* to the deterministic sentinel bytes.

    Sorted keys, compact separators, ``allow_nan=False`` — the same
    byte-determinism discipline as the results store and the trial-stats
    sidecar, so an unchanged sentinel always renders identical bytes
    (Golden Rule #4).
    """
    payload = {
        "schema_version": COMPLETION_SCHEMA_VERSION,
        "tuple_digest": sentinel.tuple_digest,
        "store_content_hash": sentinel.store_content_hash,
        "sidecar_written": sentinel.sidecar_written,
        "n_marked": sentinel.n_marked,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def write_completion_sentinel(path: Path, sentinel: TupleCompletionSentinel) -> Path:
    """Atomically write *sentinel* to *path* (tempfile + ``os.replace``) and return it.

    A kill mid-write never leaves a half-written sentinel: the file is either
    absent (tuple INCOMPLETE — full redo) or complete and valid. ORDERING
    CONTRACT (enforced by the :func:`~brutex.engine.sweep_run.run_persisted_sweep`
    caller, restated here because this is the only write surface): the
    sentinel is written STRICTLY LAST — after the store flush, after the
    trial-stats sidecar, after ``mark_many`` — so its presence proves the
    whole sequence completed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # The results store's atomic writer is the ONE source of the
    # tempfile+fsync+replace discipline (CLAUDE.md §11 COMMON) — whitebox
    # import, the trial-stats sidecar precedent.
    _atomic_write(path, render_completion_sentinel(sentinel))
    return path


def read_completion_sentinel(path: Path) -> TupleCompletionSentinel:
    """Read + validate a sentinel written by :func:`write_completion_sentinel`.

    Raises
    ------
    TupleCompletionError
        If *path* does not exist, is not valid JSON, is not a JSON object,
        carries an unsupported ``schema_version``, is missing a field, has a
        wrongly-typed field, or fails any :class:`TupleCompletionSentinel`
        invariant.
    """
    if not path.exists():
        raise TupleCompletionError(f"completion sentinel not found at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TupleCompletionError(
            f"completion sentinel at {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise TupleCompletionError(f"completion sentinel at {path} must be a JSON object")
    version = payload.get("schema_version")
    if version != COMPLETION_SCHEMA_VERSION:
        raise TupleCompletionError(
            f"unsupported completion-sentinel schema_version {version!r} at {path} "
            f"(expected {COMPLETION_SCHEMA_VERSION})"
        )
    missing = [key for key in _PAYLOAD_KEYS if key not in payload]
    if missing:
        raise TupleCompletionError(f"completion sentinel at {path} missing keys: {missing}")
    sidecar_written = payload["sidecar_written"]
    if not isinstance(sidecar_written, bool):
        raise TupleCompletionError(
            f"completion sentinel at {path} has a non-boolean sidecar_written: {sidecar_written!r}"
        )
    try:
        return TupleCompletionSentinel(
            tuple_digest=str(payload["tuple_digest"]),
            store_content_hash=str(payload["store_content_hash"]),
            sidecar_written=sidecar_written,
            n_marked=int(payload["n_marked"]),
        )
    except (TypeError, ValueError) as exc:
        raise TupleCompletionError(
            f"completion sentinel at {path} has a malformed field: {exc}"
        ) from exc


__all__ = [
    "COMPLETION_SCHEMA_VERSION",
    "COMPLETION_SUFFIX",
    "RESUME_STORE_GUARD_ENV",
    "TUPLE_ARTIFACT_NAMES",
    "TupleCompletionError",
    "TupleCompletionSentinel",
    "completion_sentinel_path",
    "missing_tuple_artifacts",
    "read_completion_sentinel",
    "render_completion_sentinel",
    "resume_store_guard_enabled",
    "write_completion_sentinel",
]
