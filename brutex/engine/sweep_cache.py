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
import math
import os
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Final, Self

import blake3
import structlog

from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover — typing-only imports (wheel-bound at runtime)
    from brutex.data.lmdb_membership import LmdbMembership
    from brutex.orchestrator.bloom import BloomFilter

_LOG = structlog.get_logger(__name__)

#: Default Bloom capacity for the sweep dedup cache. ~5M distinct combos at the
#: default FPR is ~21 MB of bit array — comfortably inside the §8 envelope while
#: covering a full-vocab K≤4 sweep (C(60,4)=487,635) with large headroom. Tune
#: up explicitly for a bigger sweep (Bloom FAIL_CLOSEDs past capacity).
DEFAULT_BLOOM_CAPACITY: Final[int] = 5_000_000

#: Default Bloom false-positive rate. A 1e-7 FPR means ~1 spurious L2 lookup per
#: 10M fresh combos — never a missed skip (Bloom has no false negatives), only
#: a rare wasted exact-store read.
DEFAULT_BLOOM_FPR: Final[float] = 1e-7

#: Default LMDB memory-map ceiling (MiB) for the exact store.
DEFAULT_MAP_SIZE_MB: Final[int] = 2 * 1024

#: Environment variable overriding the L1 Bloom capacity (distinct-digest
#: count) for the sweep dedup cache. Resolution order: explicit
#: ``bloom_capacity`` argument > this env var > :data:`DEFAULT_BLOOM_CAPACITY`
#: (see :func:`resolve_sweep_cache_bloom_capacity`). The paired CLI flag is
#: ``--sweep-cache-bloom-capacity``. Since the STEP-1 bounded auto-grow
#: (DEC-ENGINE-MISC-PACK-001) a within-wall >capacity resume self-heals
#: (:func:`_auto_grow_capacity`); the knob remains the way to PRE-SIZE a big
#: sweep up front — it is NOT a remedy for the above-the-16 GiB-wall refusal
#: (:class:`SweepCacheCapacityError`), where only a looser ``bloom_fpr`` or
#: the sharded / mmap'd Bloom follow-up helps.
SWEEP_CACHE_BLOOM_CAPACITY_ENV: Final[str] = "BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY"

#: Sub-directory under the cache root holding the LMDB exact-membership env.
_EXACT_SUBDIR: Final[str] = "exact"


class SweepCacheError(BruteXError):
    """Raised on a non-serialisable combo param or use of a closed cache."""


class SweepCacheCapacityError(SweepCacheError):
    """The persisted L2 exact-store cannot be faithfully mirrored into an L1 Bloom.

    Since the STEP-1 bounded auto-grow (DEC-ENGINE-MISC-PACK-001), a resume whose
    persisted digest count exceeds the configured ``bloom_capacity`` is first
    RESIZED via :func:`_auto_grow_capacity` — ``max(ceil(persisted × 1.25),
    ceil(capacity × 1.25))`` — provided the grown bit array still fits the
    :data:`brutex.orchestrator.bloom.MAX_BLOOM_BYTES` 16 GiB hardware-envelope
    wall (CLAUDE.md §8). This error is therefore raised by
    :meth:`SweepCache.open` ONLY when the grown sizing would bust that wall:
    FAIL_CLOSED, never silent (Golden Rule #5 — no silent FPR drift, no
    partially-populated Bloom). Above the wall a ``bloom_capacity`` raise cannot
    help — the remedies are loosening ``bloom_fpr`` or the sharded / mmap'd
    Bloom follow-up the wall has always named. (The pre-sizing invariant check
    in :meth:`SweepCache._rebuild_bloom_from_exact` also raises this — kept as
    defence-in-depth, unreachable from :meth:`SweepCache.open` once the
    capacity is pre-sized.)
    """


def resolve_sweep_cache_map_mb(
    explicit: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the SweepCache LMDB map ceiling (MiB) — an operator knob.

    Resolution order: *explicit* argument (e.g. the ``--sweep-cache-map-mb``
    CLI flag) > the ``BRUTEX_SWEEP_CACHE_MAP_MB`` environment variable
    (:data:`brutex.data.lmdb_membership.SWEEP_CACHE_MAP_MB_ENV`, the ONE
    shared knob name) > :data:`DEFAULT_MAP_SIZE_MB`. Delegates the parse /
    validation to the COMMON
    :func:`brutex.data.lmdb_membership.resolve_positive_int_env` (Rule 11
    COMMON — one resolver, engine imports from data).

    Parameters
    ----------
    explicit
        The explicit operator value, or ``None`` to consult the environment.
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.

    Returns
    -------
    int
        The resolved map ceiling in MiB (this module's
        :data:`DEFAULT_MAP_SIZE_MB` when no knob is set).

    Raises
    ------
    ValueError
        If the explicit value or the env-var value is not a positive integer
        (loud, naming the env var — never a silent fallback).
    """
    # Imported lazily: lmdb_membership pulls the runtime-only ``lmdb`` wheel;
    # this module must stay importable without it (see :meth:`SweepCache.open`).
    from brutex.data.lmdb_membership import SWEEP_CACHE_MAP_MB_ENV, resolve_positive_int_env

    resolved = resolve_positive_int_env(SWEEP_CACHE_MAP_MB_ENV, explicit, env=env)
    return DEFAULT_MAP_SIZE_MB if resolved is None else resolved


def resolve_sweep_cache_bloom_capacity(
    explicit: int | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the SweepCache L1 Bloom capacity — an operator knob.

    Resolution order: *explicit* argument (e.g. the
    ``--sweep-cache-bloom-capacity`` CLI flag) >
    :data:`SWEEP_CACHE_BLOOM_CAPACITY_ENV` > :data:`DEFAULT_BLOOM_CAPACITY`.
    The actionable remedy for the FAIL_CLOSED >capacity resume refusal
    (:class:`SweepCacheCapacityError`).

    Parameters
    ----------
    explicit
        The explicit operator value, or ``None`` to consult the environment.
    env
        Environment mapping (defaults to :data:`os.environ`) — injectable for
        tests.

    Returns
    -------
    int
        The resolved Bloom capacity (:data:`DEFAULT_BLOOM_CAPACITY` when no
        knob is set).

    Raises
    ------
    ValueError
        If the explicit value or the env-var value is not a positive integer
        (loud, naming the env var — never a silent fallback).
    """
    # Imported lazily — same runtime-only-wheel rationale as
    # :func:`resolve_sweep_cache_map_mb` (the COMMON resolver lives in data/).
    from brutex.data.lmdb_membership import resolve_positive_int_env

    resolved = resolve_positive_int_env(SWEEP_CACHE_BLOOM_CAPACITY_ENV, explicit, env=env)
    return DEFAULT_BLOOM_CAPACITY if resolved is None else resolved


def _auto_grow_capacity(configured_capacity: int, persisted: int, fpr_target: float) -> int:
    """Resolve the EFFECTIVE resume Bloom capacity — STEP-1 bounded auto-grow.

    DEC-ENGINE-MISC-PACK-001: a resume whose persisted L2 exact-store already
    holds more digests than the configured ``bloom_capacity`` used to
    FAIL_CLOSED unconditionally, forcing a manual knob turn on every organic
    overflow. STEP 1 grows the capacity AUTOMATICALLY — but only inside the
    hardware envelope:

    * ``persisted <= configured_capacity`` → the configured capacity,
      byte-identical behaviour (no grow, no log);
    * otherwise → ``grown = max(ceil(persisted × 1.25),
      ceil(configured_capacity × 1.25))`` — the ``ceil(persisted × 1.25)`` arm
      leaves HEADROOM ABOVE *persisted* (25%) so the resume rebuild fills the
      Bloom to *persisted* insertions with room to spare and the FIRST new
      ``record_digest`` does NOT trip :class:`BloomCapacityExceeded` (the C6
      resume-wedge fix; a bare ``max(persisted, …)`` sized the grown Bloom to
      EXACTLY *persisted*, so once ``persisted >= 1.25 × capacity`` a resumed
      sweep made only ONE tuple of forward progress per process restart). IFF
      the grown sizing fits the
      :data:`brutex.orchestrator.bloom.MAX_BLOOM_BYTES` 16 GiB wall
      (:func:`brutex.orchestrator.bloom._compute_m_bytes` — the pure sizing
      helper exposed for exactly this audit). A grow is LOUD: one
      ``sweep_cache.bloom_auto_grow`` structlog event names the deviation from
      the operator knob (Golden Rules #1/#8 — the knob was exceeded, say so).

    Above the wall the refusal STAYS (:class:`SweepCacheCapacityError`,
    FAIL_CLOSED — Golden Rule #5): a larger in-RAM Bloom would break the §8
    envelope, and a ``bloom_capacity`` raise cannot help — the message names
    the honest remedies (loosen ``bloom_fpr``, or the sharded / mmap'd Bloom
    follow-up). BLAST RADIUS (honest, Golden Rule #10): the production
    MASK-digest path never builds a Bloom at all (``brutex.cli`` passes
    ``plan_launch_dedup(grid, digest_path=True)`` — a no-alloc raising sink);
    the only production Bloom is the TUPLE-tier :meth:`SweepCache.open`, so
    this grow affects the tuple-resume path only.

    Parameters
    ----------
    configured_capacity
        The operator-resolved ``bloom_capacity`` knob value.
    persisted
        The persisted L2 exact-store digest count (``len(exact)``).
    fpr_target
        The Bloom false-positive-rate target the filter will be built with.

    Returns
    -------
    int
        The effective capacity to build the resume Bloom with — always
        ``>= persisted``.

    Raises
    ------
    SweepCacheCapacityError
        When the grown capacity's bit array would exceed the 16 GiB wall.
    """
    if persisted <= configured_capacity:
        return configured_capacity
    # Imported lazily — bloom pulls the runtime-only fastbloom_rs wheel; this
    # module must stay importable without it (same rationale as the resolvers).
    from brutex.orchestrator.bloom import MAX_BLOOM_BYTES, _compute_m_bytes

    grown = max(math.ceil(persisted * 1.25), math.ceil(configured_capacity * 1.25))
    m_bytes = _compute_m_bytes(grown, fpr_target)
    if m_bytes > MAX_BLOOM_BYTES:
        raise SweepCacheCapacityError(
            f"resume: persisted exact-store holds {persisted:_} digests; the auto-grown "
            f"bloom_capacity {grown:_} needs a {m_bytes:_}-byte bit array, exceeding "
            f"MAX_BLOOM_BYTES={MAX_BLOOM_BYTES:_} (the 16 GiB hardware-envelope wall, "
            f"CLAUDE.md §8) — FAIL_CLOSED (Golden Rule #5, no partially-populated Bloom). "
            f"Raising --sweep-cache-bloom-capacity / {SWEEP_CACHE_BLOOM_CAPACITY_ENV} "
            f"cannot help above the wall: loosen bloom_fpr, or wait for the sharded / "
            f"mmap'd Bloom follow-up."
        )
    _LOG.warning(
        "sweep_cache.bloom_auto_grow",
        persisted=persisted,
        configured_capacity=configured_capacity,
        grown_capacity=grown,
        m_bytes=m_bytes,
    )
    return grown


#: The three Golden-Rule-#4 reproducibility keys, RESERVED inside ``params``:
#: they are structural keyword-only parameters of :func:`combo_digest`
#: (DEC-COMBO-DIGEST-REPRO-HASHES-001) and are injected into the canonical
#: payload internally, so a caller-supplied ``params`` carrying any of them
#: would be a second, conflicting channel — refused loudly, never merged.
_RESERVED_REPRO_KEYS = frozenset({"data_snapshot_hash", "indicator_version", "code_commit_sha"})


def combo_digest(
    mask: int,
    instrument: str,
    tf: str,
    direction: str,
    params: Mapping[str, Any] | None = None,
    *,
    data_snapshot_hash: str,
    indicator_version: str,
    code_commit_sha: str,
) -> bytes:
    """Return the 32-byte BLAKE3 digest of a combo identity.

    The digest is the dedup key (Bloom + LMDB). It is deterministic and
    order-independent in *params* (keys are sorted), so the same logical combo
    always maps to the same key across runs (Golden Rule #4 reproducibility).

    **The three reproducibility hashes are STRUCTURAL, required keyword-only
    parameters (DEC-COMBO-DIGEST-REPRO-HASHES-001).** Golden Rule #4's
    run-identity formula requires ``data_snapshot_hash`` / ``indicator_version``
    / ``code_commit_hash`` in every run identity; before this change the dedup
    key carried them only by CONVENTION (the supervisor injected them into
    ``params``), so a caller that forgot silently ALIASED dedup entries across
    different data snapshots — a combo evaluated on OLD data would be skipped on
    NEW data (a stale-skip bug). Omitting any of the three is now a
    ``TypeError`` at the call site and a type-check failure, never a silent
    alias. Internally the three are injected into the canonical params payload
    under the SAME keys the legacy convention used (``data_snapshot_hash`` /
    ``indicator_version`` / ``code_commit_sha``), so for identical logical
    inputs the digest is BYTE-IDENTICAL to the pre-change convention — existing
    Bloom/LMDB dedup stores are NOT invalidated (golden-pinned in
    ``tests/unit/test_engine_sweep_cache.py``).

    Parameters
    ----------
    mask
        The strategy mask (which of the 60 conditions the combo requires).
    instrument, tf, direction
        Context labels (e.g. ``"NIFTY"``, ``"5m"``, ``"bull"``).
    params
        The combo's parameter dict (stop/target/proximity/…), or ``None`` for
        the no-parameter combo. Must be JSON-serialisable and must NOT carry
        any of the three reserved reproducibility keys.
    data_snapshot_hash
        The full-content ``bxsnap2`` digest of every loaded bar (Golden
        Rule #4; ``stage3_wire.data_snapshot_hash``).
    indicator_version
        The indicator-vocabulary version stamp.
    code_commit_sha
        The code commit SHA — Golden Rule #4's ``code_commit_hash`` leg, named
        ``code_commit_sha`` to match ``SweepGridConfig.code_commit_sha`` and
        the legacy canonical-params key (byte-identity).

    Returns
    -------
    bytes
        The raw 32-byte BLAKE3 digest.

    Raises
    ------
    SweepCacheError
        If *params* contains a value that is not JSON-serialisable, a
        non-finite float (``NaN`` / ``inf`` / ``-inf``), or any of the three
        RESERVED reproducibility keys (the structural parameters are the ONE
        channel — a second in-``params`` channel could silently disagree).

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
    merged: dict[str, Any] = dict(params) if params else {}
    overlap = _RESERVED_REPRO_KEYS.intersection(merged)
    if overlap:
        raise SweepCacheError(
            "combo params carry RESERVED reproducibility key(s) "
            f"{sorted(overlap)!r}: data_snapshot_hash / indicator_version / "
            "code_commit_sha are structural keyword-only parameters of "
            "combo_digest (DEC-COMBO-DIGEST-REPRO-HASHES-001) — pass them "
            "there, never inside params (two channels could silently disagree)"
        )
    # Injected under the SAME keys the pre-change convention placed inside
    # canonical_params, so the serialisation — and therefore the digest — is
    # byte-for-byte identical for identical logical inputs (dedup stores and
    # run identities are NOT invalidated; golden-pinned).
    merged["data_snapshot_hash"] = data_snapshot_hash
    merged["indicator_version"] = indicator_version
    merged["code_commit_sha"] = code_commit_sha
    try:
        canonical = json.dumps(
            {
                "mask": int(mask),
                "instrument": instrument,
                "tf": tf,
                "direction": direction,
                "params": merged,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except TypeError as exc:
        raise SweepCacheError(f"combo params are not JSON-serialisable: {exc}") from exc
    except ValueError as exc:
        raise SweepCacheError(
            "combo params are not JSON-serialisable as a canonical digest "
            "(non-finite float such as NaN/inf, or a circular reference): "
            f"{exc}"
        ) from exc
    return blake3.blake3(canonical.encode("utf-8")).digest()


class SweepCache:
    """Bloom + LMDB O(1) membership dedup for sweep combos.

    Open via :meth:`open` (a context manager); the caller owns the lifetime.
    Thread-safe: :meth:`seen_digest` and :meth:`record_digest` each hold
    ``self._lock`` across the ENTIRE probe-then-act (the Bloom probe + the L2
    exact-store confirm/insert + the counter bumps), so concurrent sweep workers
    can share one cache instance and a given digest is admitted to the Bloom
    exactly once — never double-counted against the FAIL_CLOSED capacity nor
    double-evaluated. The critical section does no vendor / network I/O (the LMDB
    point get/put are O(1)), so the hold time stays O(1) (mirrors
    :meth:`brutex.data.option_dedup.OptionDedupIndex.add_if_new`).

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
        bloom_capacity: int | None = None,
        bloom_fpr: float = DEFAULT_BLOOM_FPR,
        map_size_mb: int | None = None,
    ) -> SweepCache:
        """Open (creating if absent) a sweep dedup cache rooted at *path*.

        The L1 Bloom filter is in-RAM; the L2 LMDB exact store persists under
        ``path / "exact"`` so a re-run reloads the ground-truth membership and
        resumes skipping. On a **resume** (a non-empty exact store) the fresh
        in-RAM Bloom is **repopulated from the persisted digests** — see
        :meth:`_rebuild_bloom_from_exact` — so a Bloom-negative can never mask a
        combo the previous run already recorded (the resume-dedup guarantee,
        CLAUDE.md §2 Rules 4 & 7). Without the rebuild, ``seen_digest`` would
        treat every persisted digest as Bloom-negative and re-run the entire
        prior sweep.

        **STEP-1 bounded auto-grow (DEC-ENGINE-MISC-PACK-001):** the exact store
        is opened FIRST and the Bloom capacity is pre-sized from its persisted
        count via :func:`_auto_grow_capacity` — a resume holding MORE digests
        than the configured ``bloom_capacity`` auto-grows to
        ``max(ceil(persisted × 1.25), ceil(capacity × 1.25))`` (one loud
        ``sweep_cache.bloom_auto_grow`` event) — HEADROOM above *persisted* so
        the resumed sweep can record NEW tuples, not just rebuild — instead of
        refusing, provided
        the grown bit array fits the 16 GiB
        :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES` wall; above the wall
        the FAIL_CLOSED refusal stays, naming the sharded / mmap'd Bloom
        follow-up. A within-capacity resume is byte-identical to before.

        Parameters
        ----------
        path
            Cache root directory (created with parents).
        bloom_capacity
            L1 Bloom capacity — an operator knob (R2-SL-F4-F5): explicit value
            > :data:`SWEEP_CACHE_BLOOM_CAPACITY_ENV` >
            :data:`DEFAULT_BLOOM_CAPACITY`
            (:func:`resolve_sweep_cache_bloom_capacity`).
        bloom_fpr
            L1 false-positive-rate target (see :data:`DEFAULT_BLOOM_FPR`).
        map_size_mb
            L2 LMDB memory-map ceiling in MiB — an operator knob: explicit
            value > ``BRUTEX_SWEEP_CACHE_MAP_MB`` > :data:`DEFAULT_MAP_SIZE_MB`
            (:func:`resolve_sweep_cache_map_mb`).

        Raises
        ------
        SweepCacheCapacityError
            If the persisted exact store holds more digests than the resolved
            ``bloom_capacity`` AND the auto-grown capacity's bit array would
            exceed the 16 GiB :data:`~brutex.orchestrator.bloom.MAX_BLOOM_BYTES`
            wall (:func:`_auto_grow_capacity`). FAIL_CLOSED (Golden Rule #5, no
            silent FPR drift); the message names the honest above-the-wall
            remedies (loosen ``bloom_fpr`` / the sharded follow-up). The
            just-opened exact store is closed before the raise so the refused
            open leaks no LMDB environment.
        ValueError
            If either knob (explicit or env) is not a positive integer.
        LmdbCorruptEnvError
            If the L2 exact store's ``data.mdb`` is corrupt AND a SECOND
            open fails after the quarantine-and-rebuild-once recovery
            (DEC-SWEEP-LMDB-QUARANTINE-REBUILD-001, MIGRATION.md #1795). A
            FIRST corruption is recovered transparently (quarantined aside,
            rebuilt empty) — the store is a rebuildable dedup accelerator, so
            an emptied set only ever causes a wasted recompute, never a wrong
            result (Golden Rule #7).
        """
        # Imported lazily: both pull runtime-only deps (fastbloom_rs / lmdb) and
        # keep this module importable (for the per-module coverage gate) without
        # touching the orchestrator package init chain at import time.
        import lmdb

        from brutex.data.lmdb_membership import LmdbCorruptEnvError

        resolved_capacity = resolve_sweep_cache_bloom_capacity(bloom_capacity)
        resolved_map_mb = resolve_sweep_cache_map_mb(map_size_mb)
        path.mkdir(parents=True, exist_ok=True)
        exact_dir = path / _EXACT_SUBDIR
        try:
            bloom, exact = cls._open_exact_and_bloom(
                exact_dir, resolved_capacity, bloom_fpr, resolved_map_mb
            )
        except (LmdbCorruptEnvError, lmdb.Error) as exc:
            # The L2 exact store is a losslessly-rebuildable dedup ACCELERATOR:
            # an emptied membership set can only ever cause a wasted recompute,
            # never a wrong result (the recorded combos re-run + re-record; the
            # ResultsStore idempotence keystone re-flushes byte-identical rows,
            # Golden Rule #7). So quarantine the corrupt env aside (bytes
            # PRESERVED, never deleted — lake_heal philosophy) and rebuild a
            # fresh empty store ONCE. A SECOND failure propagates typed (below).
            # A wall refusal / Bloom config error (SweepCacheCapacityError) is
            # NOT an LMDB corruption and is NOT caught here — it still propagates
            # as before via `_open_exact_and_bloom`'s own env-leak guard.
            quarantined_to = cls._quarantine_corrupt_exact(exact_dir)
            if quarantined_to is not None:
                _LOG.warning(
                    "sweep_cache.corrupt_quarantined",
                    path=str(exact_dir),
                    quarantined_to=str(quarantined_to),
                    error_type=type(exc).__name__,
                    note=(
                        "dedup accelerator rebuilt empty — affected tuples recompute, "
                        "results byte-identical"
                    ),
                )
            bloom, exact = cls._open_exact_and_bloom(
                exact_dir, resolved_capacity, bloom_fpr, resolved_map_mb
            )
        return cls(bloom=bloom, exact=exact)

    @classmethod
    def _open_exact_and_bloom(
        cls,
        exact_dir: Path,
        resolved_capacity: int,
        bloom_fpr: float,
        resolved_map_mb: int,
    ) -> tuple[BloomFilter, LmdbMembership]:
        """Open the L2 exact store + build the pre-sized L1 Bloom for ONE attempt.

        The single open+rebuild unit both :meth:`open` and its
        quarantine-and-rebuild retry call. A corrupt ``data.mdb`` surfaces here
        as an :class:`~brutex.data.lmdb_membership.LmdbCorruptEnvError` (at the
        ``LmdbMembership.open`` boundary) or a raw ``lmdb.Error`` (a mid-rebuild
        cursor/read fault); the env is never leaked on any post-open failure.
        """
        from brutex.data.lmdb_membership import LmdbMembership
        from brutex.orchestrator.bloom import BloomFilter

        exact = LmdbMembership.open(exact_dir, map_size_mb=resolved_map_mb)
        try:
            # Pre-size the Bloom from the persisted ground truth BEFORE building
            # it (STEP-1 auto-grow, DEC-ENGINE-MISC-PACK-001) — the wall refusal
            # happens here, never mid-rebuild.
            effective_capacity = _auto_grow_capacity(resolved_capacity, len(exact), bloom_fpr)
            bloom = BloomFilter(capacity=effective_capacity, fpr_target=bloom_fpr)
            cls._rebuild_bloom_from_exact(bloom, exact, bloom_capacity=effective_capacity)
        except Exception:
            # Never leak the LMDB env on a refused open (a wall refusal, a Bloom
            # config error, a mid-rebuild lmdb fault) — the operator's very next
            # action is re-opening the SAME path. Narrowly re-raised as-is.
            exact.close()
            raise
        return bloom, exact

    @staticmethod
    def _quarantine_corrupt_exact(exact_dir: Path) -> Path | None:
        """Atomically rename a corrupt exact-store dir aside; return the new path.

        The bytes are PRESERVED (renamed, never deleted — the lake_heal
        philosophy) to a sibling ``<name>.corrupt-<ns>`` whose nanosecond stamp
        makes repeated quarantines collision-free. A fresh empty exact store is
        then rebuilt at the original path by :meth:`open` (recompute-safe,
        Golden Rule #7).

        **Crash idempotence:** the rename is atomic. A crash AFTER the rename
        leaves the original path absent → the next open creates a fresh env; a
        crash BEFORE it re-detects the same corruption on the next open and
        re-quarantines it under a NEW nanosecond stamp.

        **Rename-failure belt (review round 1):** an ``OSError`` from the rename
        itself — e.g. ``FileNotFoundError`` when the env dir VANISHED under us (a
        concurrent quarantine won the race) — logs one
        ``sweep_cache.quarantine_rename_failed`` WARN and returns ``None`` so
        :meth:`open` continues to the reopen attempt instead of dying on a raw
        traceback: a vanished dir reopens FRESH (nothing left to quarantine); a
        still-in-place corrupt env (``EACCES`` etc.) re-detects on the reopen and
        the SECOND failure propagates TYPED as before.
        """
        quarantined = exact_dir.with_name(f"{exact_dir.name}.corrupt-{time.time_ns()}")
        try:
            os.rename(exact_dir, quarantined)
        except OSError as exc:
            _LOG.warning(
                "sweep_cache.quarantine_rename_failed",
                path=str(exact_dir),
                quarantined_to=str(quarantined),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return None
        return quarantined

    @staticmethod
    def _rebuild_bloom_from_exact(
        bloom: BloomFilter,
        exact: LmdbMembership,
        *,
        bloom_capacity: int,
    ) -> None:
        """Repopulate a fresh *bloom* from every digest persisted in *exact*.

        Called once by :meth:`open` on a resume so the in-RAM L1 Bloom reflects
        the durable L2 ground truth. O(persisted) — a resume pays it exactly once
        at startup. On a cold start (empty exact store) it is a no-op. The count
        is checked FIRST against ``bloom_capacity`` so a persisted set larger than
        the Bloom can hold FAILS_CLOSED with :class:`SweepCacheCapacityError`
        rather than tripping the Bloom's own :class:`BloomCapacityExceeded`
        part-way through the rebuild (which would leave a partially-populated
        Bloom). Since the STEP-1 auto-grow (DEC-ENGINE-MISC-PACK-001) :meth:`open`
        pre-sizes ``bloom_capacity >= persisted`` via :func:`_auto_grow_capacity`,
        so this guard is DEFENCE-IN-DEPTH — unreachable from :meth:`open`, kept
        as the invariant check for any direct caller.
        """
        persisted = len(exact)
        if persisted == 0:
            return
        if persisted > bloom_capacity:
            raise SweepCacheCapacityError(
                f"resume: persisted exact-store holds {persisted:_} digests but "
                f"bloom_capacity is {bloom_capacity:_} — re-open with a larger "
                f"bloom_capacity (raise --sweep-cache-bloom-capacity or "
                f"{SWEEP_CACHE_BLOOM_CAPACITY_ENV}) so the Bloom can faithfully "
                f"reflect every recorded combo (FAIL_CLOSED, Golden Rule #5)."
            )
        for digest in exact.iter_keys():
            bloom.add(digest)

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

    def _seen_digest_locked(self, digest: bytes) -> bool:
        """Membership probe — caller MUST hold ``self._lock`` (the shared logic).

        L1 Bloom first: a Bloom ``False`` is conclusive (no false negatives) so
        the combo is *definitely new* and we skip the LMDB read; a Bloom ``True``
        falls through to the L2 exact store for the ground-truth answer. The
        LMDB point-get is O(1) and does no vendor / network I/O, so holding the
        in-process lock across it keeps the hold time O(1).
        """
        self._checks += 1
        if not self._bloom.might_contain(digest):
            self._bloom_negatives += 1
            return False
        present = digest in self._exact
        if present:
            self._hits += 1
        return present

    def seen_digest(self, digest: bytes) -> bool:
        """Return ``True`` iff *digest* has already been recorded — O(1), atomic.

        The Bloom probe and the L2 exact-store confirm are ONE critical section,
        so a concurrent :meth:`record_digest` of the same digest can never leave
        the two tiers observed in an inconsistent interleaving (the docstring's
        thread-safe-sharing promise, mirroring
        :meth:`brutex.data.option_dedup.OptionDedupIndex.add_if_new`).
        """
        self._require_open()
        with self._lock:
            return self._seen_digest_locked(digest)

    def record_digest(self, digest: bytes) -> bool:
        """Mark *digest* as evaluated in both tiers — O(1), atomic.

        Returns ``True`` if it was newly recorded, ``False`` if it was already
        present (an idempotent re-record is a no-op that does not re-insert into
        the Bloom, so duplicate inserts never erode the FPR — CLAUDE.md §2
        Rule 7).

        **Durability ordering — exact-first (R2-SL-F4-F5).** The durable L2
        ground truth is written BEFORE the approximate L1 accelerator: a
        write fault during ``exact.add`` (e.g. a map-full
        :class:`~brutex.data.lmdb_membership.LmdbMapFullError`) propagates
        with the Bloom UNTOUCHED, so no phantom Bloom entry can outlive the
        failure. The idempotence guard stays honest — a retry after growing
        the map re-records cleanly (no double-count against the FAIL_CLOSED
        Bloom capacity, no false-positive skip of a never-persisted combo).
        The Bloom has no delete, so the reverse order could never be rolled
        back. (The Bloom-first sibling
        :meth:`brutex.data.option_dedup.OptionDedupIndex._add_locked` still
        carries the old ordering — a flagged Rule-11 COMMON follow-up, not
        changed here.)

        The exact-store probe, the exact-store insert, and the Bloom insert are
        ONE critical section held under ``self._lock``, so two concurrent workers
        recording the SAME new digest can never both pass the probe and both
        insert — the digest is admitted to the Bloom EXACTLY once (no
        double-count against the FAIL_CLOSED Bloom capacity, no double-evaluation
        of the tuple). The LMDB get/put are O(1) and I/O-free at the Python
        layer, so the hold time is O(1).
        """
        self._require_open()
        with self._lock:
            if digest in self._exact:
                return False
            self._exact.add(digest)
            self._bloom.add(digest)
            self._records += 1
            return True

    # ── Combo-keyed convenience ────────────────────────────────────────────

    def seen(
        self,
        mask: int,
        instrument: str,
        tf: str,
        direction: str,
        params: Mapping[str, Any] | None = None,
        *,
        data_snapshot_hash: str,
        indicator_version: str,
        code_commit_sha: str,
    ) -> bool:
        """Return ``True`` iff this combo has already been evaluated — O(1).

        The three reproducibility hashes are structurally REQUIRED — see
        :func:`combo_digest` (DEC-COMBO-DIGEST-REPRO-HASHES-001): a caller that
        omitted them would silently alias dedup entries across data snapshots.
        """
        return self.seen_digest(
            combo_digest(
                mask,
                instrument,
                tf,
                direction,
                params,
                data_snapshot_hash=data_snapshot_hash,
                indicator_version=indicator_version,
                code_commit_sha=code_commit_sha,
            )
        )

    def record(
        self,
        mask: int,
        instrument: str,
        tf: str,
        direction: str,
        params: Mapping[str, Any] | None = None,
        *,
        data_snapshot_hash: str,
        indicator_version: str,
        code_commit_sha: str,
    ) -> bool:
        """Mark this combo evaluated; return ``True`` if newly recorded — O(1).

        The three reproducibility hashes are structurally REQUIRED — see
        :func:`combo_digest` (DEC-COMBO-DIGEST-REPRO-HASHES-001).
        """
        return self.record_digest(
            combo_digest(
                mask,
                instrument,
                tf,
                direction,
                params,
                data_snapshot_hash=data_snapshot_hash,
                indicator_version=indicator_version,
                code_commit_sha=code_commit_sha,
            )
        )

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
    "SWEEP_CACHE_BLOOM_CAPACITY_ENV",
    "SweepCache",
    "SweepCacheCapacityError",
    "SweepCacheError",
    "combo_digest",
    "resolve_sweep_cache_bloom_capacity",
    "resolve_sweep_cache_map_mb",
]
