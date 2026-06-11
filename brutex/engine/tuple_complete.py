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
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.engine.results_store import _atomic_write
from brutex.engine.trial_stats import TrialStatsError, _validate_hex
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
    "TupleCompletionError",
    "TupleCompletionSentinel",
    "completion_sentinel_path",
    "read_completion_sentinel",
    "render_completion_sentinel",
    "write_completion_sentinel",
]
