"""O(1) combo-dedup cache for the brute-force sweep (DEC-SWEEP-REALTIME-GUARANTEE-001).

So a re-run **skips already-evaluated combos** instead of recomputing them
(CLAUDE.md §2 Rule 7 — idempotence). A combo's identity is the tuple
``(mask, instrument, tf, direction, params)``; this cache records WHICH such
combos have already been evaluated (their ``evaluate → ResultMetrics`` step has
run), so the driver can short-circuit a cache hit. It is the sweep-side
realisation of the 4-tier dedup discipline (CLAUDE.md §2 Rule 11):

* **L1 — Bloom pre-filter** (:class:`brutex.orchestrator.bloom.BloomFilter`).
  Sub-µs probabilistic gate. A ``False`` means *definitely never seen* (the
  fast path that lets a fresh combo skip the LMDB read entirely); a ``True``
  means *maybe seen* → fall through to L2.
* **L2 — LMDB exact store** (:class:`brutex.data.lmdb_membership.LmdbMembership`).
  Off-heap, memory-mapped ground truth; ``__contains__`` is O(1) for a dense
  working set, and membership lives off the Python heap so per-op RAM is O(1)
  regardless of how many combos are cached.

The cache key is the 32-byte **BLAKE3** digest of the canonicalised combo
identity, used bit-identically for both the Bloom add/check and the LMDB
key. BLAKE3 comes from the wheel-free ``blake3`` package (this module must run +
be 100%-tested without the Rust kernel), the same algorithm as
:func:`brutex.strategies.combo.combo_hash`'s ``fno_math.blake3_hash``.

## Scope — membership dedup, not a result store (honest envelope, Golden Rule #10)

This is a **membership** tier: it answers "has this combo been evaluated?" so
the expensive backtest is skipped on a hit. It deliberately does **not** store
the :class:`~brutex.metrics.compute.ResultMetrics` value — that is the
:class:`brutex.engine.results_store.ResultsStore`'s job (keyed by the same
combo identity), and pulling it in here would drag the Rust-wheel-bound
``brutex.strategies`` / ``brutex.metrics`` import chain into this wheel-free
module. Retrieving the cached metrics on a hit (vs only skipping the recompute)
is a Wave-2 extension layered on the same key space.

Each operation is **O(1)** (one Bloom check + at most one LMDB point get/put)
with **bounded** memory (Bloom sized by capacity; LMDB off-heap). No claim is
made about O(1) total time/space for a whole sweep — that is impossible; the
LMDB store grows O(N) in the number of distinct combos retained.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import blake3

from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (wheel-bound at runtime)
    from brutex.data.lmdb_membership import LmdbMembership
    from brutex.orchestrator.bloom import BloomFilter

#: Default Bloom capacity for the sweep dedup cache. ~5M distinct combos at the
#: default FPR is ~21 MB of bit array — comfortably inside the §8 envelope while
#: covering a full-vocab K≤4 sweep (C(52,4)=270,725) with large headroom. Tune
#: up explicitly for a bigger sweep (Bloom FAIL_CLOSEDs past capacity).
DEFAULT_BLOOM_CAPACITY: Final[int] = 5_000_000

#: Default Bloom false-positive rate. A 1e-7 FPR means ~1 spurious L2 lookup per
#: 10M fresh combos — never a missed skip (Bloom has no false negatives), only
#: a rare wasted exact-store read.
DEFAULT_BLOOM_FPR: Final[float] = 1e-7

#: Default LMDB memory-map ceiling (MiB) for the exact store.
DEFAULT_MAP_SIZE_MB: Final[int] = 2 * 1024

#: Sub-directory under the cache root holding the LMDB exact-membership env.
_EXACT_SUBDIR: Final[str] = "exact"


class SweepCacheError(BruteXError):
    """Raised on a non-serialisable combo param or use of a closed cache."""


def combo_digest(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    params: Mapping[str, Any] | None = None,
) -> bytes:
    """Return the 32-byte BLAKE3 digest of a combo identity.

    The digest is the dedup key (Bloom + LMDB). It is deterministic and
    order-independent in *params* (keys are sorted), so the same logical combo
    always maps to the same key across runs (Golden Rule #4 reproducibility).

    Parameters
    ----------
    mask
        The u64 strategy mask (which of the 52 conditions the combo requires).
    instrument, tf, direction
        Context labels (e.g. ``"NIFTY"``, ``"5m"``, ``"bull"``).
    params
        The combo's parameter dict (stop/target/proximity/…), or ``None`` for
        the no-parameter combo. Must be JSON-serialisable.

    Returns
    -------
    bytes
        The raw 32-byte BLAKE3 digest.

    Raises
    ------
    SweepCacheError
        If *params* contains a value that is not JSON-serialisable, or a
        non-finite float (``NaN`` / ``inf`` / ``-inf``).

    Notes
    -----
    ``allow_nan=False`` rejects non-finite floats (``NaN`` / ``inf`` /
    ``-inf``) so the dedup-key canonicalisation obeys the SAME one-NaN-policy
    (CLAUDE.md §2 Rule 11 COMMON) as the two sibling canonicalisers:
    :func:`brutex.strategies.combo.canonicalise_params` (run-identity hash,
    rejects ``NaN``) and
    :func:`brutex.engine.tuple_complete.render_completion_sentinel`
    (completion sentinel, ``allow_nan=False``). Without it the default
    ``json.dumps`` would silently emit the non-standard ``NaN`` / ``Infinity``
    tokens — a membership key the run-identity hash would have refused. The
    guard is defensive (the engine feeds no non-finite params today); for any
    FINITE input the serialisation — and therefore the digest — is byte-for-byte
    identical to before (``allow_nan=False`` only ever rejects, never reshapes a
    finite value).
    """
    try:
        canonical = json.dumps(
            {
                "mask": int(mask),
                "instrument": instrument,
                "tf": tf,
                "direction": direction,
                "params": dict(params) if params else {},
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except TypeError as exc:
        raise SweepCacheError(f"combo params are not JSON-serialisable: {exc}") from exc
    except ValueError as exc:
        raise SweepCacheError(f"combo params contain a non-finite float: {exc}") from exc
    return blake3.blake3(canonical.encode("utf-8")).digest()


class SweepCache:
    """Bloom + LMDB O(1) membership dedup for sweep combos.

    Open via :meth:`open` (a context manager); the caller owns the lifetime.
    Thread-safe: the Bloom writer is internally locked, the LMDB transactions
    are atomic, and the in-process counters are guarded by a lock so concurrent
    sweep workers can share one cache instance.

    Parameters
    ----------
    bloom
        The L1 :class:`~brutex.orchestrator.bloom.BloomFilter`.
    exact
        The L2 :class:`~brutex.data.lmdb_membership.LmdbMembership` store.
    """

    def __init__(self, *, bloom: BloomFilter, exact: LmdbMembership) -> None:
        self._bloom = bloom
        self._exact = exact
        self._closed = False
        self._lock = threading.Lock()
        # O(1) assurance counters.
        self._checks = 0
        self._bloom_negatives = 0
        self._hits = 0
        self._records = 0

    @classmethod
    def open(
        cls,
        path: Path,
        *,
        bloom_capacity: int = DEFAULT_BLOOM_CAPACITY,
        bloom_fpr: float = DEFAULT_BLOOM_FPR,
        map_size_mb: int = DEFAULT_MAP_SIZE_MB,
    ) -> SweepCache:
        """Open (creating if absent) a sweep dedup cache rooted at *path*.

        The L1 Bloom filter is in-RAM (rebuilt each run); the L2 LMDB exact
        store persists under ``path / "exact"`` so a re-run reloads the
        ground-truth membership and resumes skipping.

        Parameters
        ----------
        path
            Cache root directory (created with parents).
        bloom_capacity, bloom_fpr
            L1 sizing (see :data:`DEFAULT_BLOOM_CAPACITY` /
            :data:`DEFAULT_BLOOM_FPR`).
        map_size_mb
            L2 LMDB memory-map ceiling in MiB.
        """
        # Imported lazily: both pull runtime-only deps (fastbloom_rs / lmdb) and
        # keep this module importable (for the per-module coverage gate) without
        # touching the orchestrator package init chain at import time.
        from brutex.data.lmdb_membership import LmdbMembership
        from brutex.orchestrator.bloom import BloomFilter

        path.mkdir(parents=True, exist_ok=True)
        bloom = BloomFilter(capacity=bloom_capacity, fpr_target=bloom_fpr)
        exact = LmdbMembership.open(path / _EXACT_SUBDIR, map_size_mb=map_size_mb)
        return cls(bloom=bloom, exact=exact)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        del exc_type, exc_val, exc_tb
        self.close()

    def close(self) -> None:
        """Release the LMDB exact store (idempotent). The Bloom is in-RAM."""
        if not self._closed:
            self._exact.close()
            self._closed = True

    def _require_open(self) -> None:
        """Raise :class:`SweepCacheError` if the cache has been closed."""
        if self._closed:
            raise SweepCacheError("operation on a closed SweepCache")

    # ── Digest-keyed core ──────────────────────────────────────────────────

    def seen_digest(self, digest: bytes) -> bool:
        """Return ``True`` iff *digest* has already been recorded — O(1).

        L1 Bloom first: a Bloom ``False`` is conclusive (no false negatives) so
        the combo is *definitely new* and we skip the LMDB read. A Bloom
        ``True`` falls through to the L2 exact store for the ground-truth answer.
        """
        self._require_open()
        with self._lock:
            self._checks += 1
            if not self._bloom.might_contain(digest):
                self._bloom_negatives += 1
                return False
        # L2 ground truth (LMDB txn is its own lock-free read).
        present = digest in self._exact
        if present:
            with self._lock:
                self._hits += 1
        return present

    def record_digest(self, digest: bytes) -> bool:
        """Mark *digest* as evaluated in both tiers — O(1).

        Returns ``True`` if it was newly recorded, ``False`` if it was already
        present (an idempotent re-record is a no-op that does not re-insert into
        the Bloom, so duplicate inserts never erode the FPR — CLAUDE.md §2
        Rule 7).
        """
        self._require_open()
        if digest in self._exact:
            return False
        with self._lock:
            self._bloom.add(digest)
            self._records += 1
        self._exact.add(digest)
        return True

    # ── Combo-keyed convenience ────────────────────────────────────────────

    def seen(
        self,
        mask: int,
        instrument: str,
        tf: str,
        direction: str,
        params: Mapping[str, Any] | None = None,
    ) -> bool:
        """Return ``True`` iff this combo has already been evaluated — O(1)."""
        return self.seen_digest(combo_digest(mask, instrument, tf, direction, params))

    def record(
        self,
        mask: int,
        instrument: str,
        tf: str,
        direction: str,
        params: Mapping[str, Any] | None = None,
    ) -> bool:
        """Mark this combo evaluated; return ``True`` if newly recorded — O(1)."""
        return self.record_digest(combo_digest(mask, instrument, tf, direction, params))

    # ── Observability ──────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """Return an O(1) JSON-friendly view of the cache's assurance counters.

        ``checks`` / ``bloom_negatives`` / ``hits`` / ``records`` are the
        in-process tallies; ``bloom_insertions`` / ``bloom_bytes`` expose the
        L1 sizing for capacity-planning audits.
        """
        with self._lock:
            return {
                "checks": self._checks,
                "bloom_negatives": self._bloom_negatives,
                "hits": self._hits,
                "records": self._records,
                "bloom_insertions": self._bloom.insertions_attempted,
                "bloom_bytes": self._bloom.m_bytes,
                "closed": self._closed,
            }


__all__ = [
    "DEFAULT_BLOOM_CAPACITY",
    "DEFAULT_BLOOM_FPR",
    "DEFAULT_MAP_SIZE_MB",
    "SweepCache",
    "SweepCacheError",
    "combo_digest",
]
