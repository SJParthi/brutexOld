"""Bloom filter — L1 fast-fail primitive for the 4-tier dedup hot-path.

The :class:`BloomFilter` is the sub-microsecond probabilistic membership
gate that sits in front of :meth:`WorkItemStore.exists_terminal`
(LMDB ground-truth) in BruteX's 4-tier dedup (CLAUDE.md §2 rule 11):

* **L1 — Bloom (this module).** Rejects 99.9999%+ of duplicates before
  touching LMDB. Hot-path budget: sub-µs per check.
* **L2 — LMDB ground-truth** (:mod:`brutex.orchestrator.store`). 150 µs
  per lookup; only invoked when L1 returns "maybe seen".
* **L3 — Audit JSONL replay** (Golden Rule #7 oracle). Bit-equality test.
* **L4 — CI property test.** Runtime invariant `false_neg_rate == 0`
  (modulo concurrent-write race, see :meth:`might_contain` contract).

## Backend choice — fastbloom-rs (Rust + PyO3)

Per CLAUDE.md §4 "no pandas in hot paths — use polars or numpy + numba
or Rust" and ``docs/TRACK1_BACKTEST_SPEC.md:198`` (which explicitly
cites ``fastbloom``), the backing implementation is the published
`fastbloom-rs <https://pypi.org/project/fastbloom-rs/>`_ PyPI wheel
(Rust impl, manylinux pre-built). The wrapper in this module adds:

* **FAIL_CLOSED capacity gate.** When ``insertions_attempted >= capacity``
  the next :meth:`add` raises :class:`BloomCapacityExceeded`. Silent FPR
  drift (DEGRADED_MODE) is unacceptable per Rule #5 (O(1) discipline).
* **MAX_BLOOM_BYTES sizing guard.** Constructor rejects any
  ``(capacity, fpr_target)`` whose computed bit-array exceeds the
  RAM-derived ceiling — 1/3 of the machine's detected total RAM, the
  16 GiB DEC-P0-047 floor when RAM is undetectable, or the validated
  ``BRUTEX_MAX_BLOOM_BYTES`` override (DEC-RAM-BUDGETS-001; exact
  16 GiB parity on the CLAUDE.md §8 48 GiB box, leaving headroom for
  indicators + LMDB cache + Python). Larger sweeps must escalate to
  sharded / mmap'd Bloom or Cuckoo (deferred follow-up).
* **MIN_FPR_TARGET floor.** ``fpr_target < 1e-9`` is rejected in V1 —
  anything tighter implies sharded work that isn't shipped.
* **Hash-function agnostic.** :meth:`add` / :meth:`might_contain` take
  raw ``bytes``; the caller pre-hashes via sha256 today and blake3
  tomorrow (per DEC-P0-044 Q-02 migration). Bloom never re-hashes,
  so the migration is zero-touch.
* **Thread-safe writes.** :meth:`add` is serialised via a
  :class:`threading.Lock`; :meth:`might_contain` is lockless reads.
  See concurrent-race contract in :meth:`might_contain` docstring.
* **structlog init emission.** Every constructor logs the computed
  ``(capacity, fpr_target, m_bytes, k, bits_per_item)`` so the audit
  oracle can replay the configuration.

## Sizing math (verified against Bloom 1970 + Mitzenmacher & Upfal 2005 §5.5.3)

For target capacity ``n`` and target FPR ``p``:

  m_bits = ceil( -n * ln(p) / (ln 2)^2 )      # bits in the array
  k      = ceil(  (m_bits / n) * ln 2 )       # number of hash funcs
  bits_per_item = m_bits / n

Both formulas use ``ceil`` — truncating either silently elevates the
achieved FPR above target (caught as a CRITICAL finding by Hostile
agent §1 in the DEC-P0-047 pre-impl review).

## Sizing conflict resolution (Renaissance C-07)

``docs/TRACK1_REQUIREMENTS_FINAL.md`` §1 C-07 flags 3 conflicting
sizings in MIGRATION.md: 150M-1B (#121), 40GB at 33B (#203), Cuckoo
at 146B (#255). The 4-agent Reality-check lens reconciled:

* **150M-1B = Stage 2 survivor count (canonical for Bloom).** At FPR
  ``1e-9`` this is 5.39 GB at 1B, 0.81 GB at 150M — fits 48 GB envelope.
* **40 GB = obsolete raw-trial sizing** (superseded by post-funnel
  count).
* **146 B = Stage 1A pre-filter input** — Bloom is the wrong tool at
  that scale (would need ~787 GB). Stage 1A uses deterministic
  per-bar keys, no dedup needed; sharded follow-up tracked.

V1 Phase-1 scaffolding defaults: ``capacity=100_000_000``,
``fpr_target=1e-7`` → ~419 MB RAM. Covers the 92-chunk first-run
(~50M bar-keys) with 2× headroom. Phase 3 measurement-driven retune
to the 150M-1B target range.

## What does NOT ship in V1 (deferred to future DEC-P0-NNN)

* **Persistence / mmap'd bit array.** Bloom is approximate by design;
  cold-start rebuilds via L2 LMDB scan in
  :meth:`WorkItemStore.open` (PR-I integration). Recoverable.
* **Auto-resize.** FAIL_CLOSED instead — operator decides whether to
  bump capacity + restart.
* **Sharded / hierarchical bucketing.** Past ~3B survivors the in-RAM
  Bloom approaches the 48 GB ceiling; sharded follow-up tracked.
* **Cuckoo-filter fallback** (supports deletion). Bloom can't delete;
  if Phase 3 measures a hot delete workload, swap to Cuckoo.
* **blake3 hash backend.** Hash-agnostic via raw-bytes API; the
  sha256 → blake3 migration (DEC-P0-044 Q-02) lands in the engine PR
  without touching this module.
* **AuditWriter integration.** ``BloomCapacityExceeded`` is raised but
  the audit-action emission is the **caller's** job (PR-I will wire
  this in :class:`WorkItemStore`). Keeps Bloom as a pure primitive.

References
----------
* CLAUDE.md §2 rule 11 (4-tier dedup), §4 (Rust hot-path), §8 (48 GB)
* docs/TRACK1_BACKTEST_SPEC.md:198 (fastbloom citation)
* docs/TRACK1_REQUIREMENTS_FINAL.md §1 C-07 (sizing conflict)
* docs/TRACK1_REQUIREMENTS_FINAL.md §3 G-02 (Bloom GAP)
* docs/MIGRATION.md #121, #203, #255, #181 (DEC-P0-044 hash migration)
* Bloom (1970) "Space/Time Trade-offs in Hash Coding with Allowable Errors"
* Swamidass + Baldi (2007) "Mathematical correction for fingerprint
  similarity measures to improve chemical retrieval"  — bit-density
  unique-count estimator used by :meth:`unique_count_estimate`.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

import structlog

from brutex.hardware_envelope import memory_budget_bytes
from brutex.models import BruteXError

if TYPE_CHECKING:
    from fastbloom_rs import BloomFilter as _RustBloomFilter


def _load_rust_bloom() -> type[_RustBloomFilter]:
    """Import the ``fastbloom_rs`` Rust filter lazily, on first construction.

    Mirrors the lazy ``fno_math`` import in :func:`_popcount_bytes`: merely
    importing this module — e.g. when ``pytest-cov`` pre-imports
    ``brutex.orchestrator.*`` for a *standalone* strict per-module coverage
    gate — must NOT initialise the ``fastbloom_rs`` PyO3 extension. That wheel
    is built ``abi3-py38`` (``cp37-abi3``), and PyO3 forbids initialising such
    a module more than once per interpreter process; an import-time init
    collides with the coverage machinery's own source-package import and aborts
    the gate with *"PyO3 modules compiled for CPython 3.8 or older may only be
    initialized once per interpreter process"*. Deferring the import to the one
    site that actually needs the class (``BloomFilter.__post_init__``) keeps the
    package import side-effect-free, so the per-module gate can be re-enabled
    (DEC-ORCH-LAZY-BLOOM-001). The real-filter behaviour is unchanged: the very
    first ``BloomFilter()`` triggers exactly one init, identical to the prior
    eager top-level import.
    """
    from fastbloom_rs import BloomFilter as RustBloomFilter

    # mypy treats fastbloom_rs as Any (ignore_missing_imports override) while
    # pyright resolves the real class; the explicit local annotation narrows the
    # Any for mypy (no `no-any-return`) without a `cast` that pyright would flag
    # as unnecessary (reportUnnecessaryCast) — satisfying BOTH type checkers.
    cls: type[_RustBloomFilter] = RustBloomFilter
    return cls


def _popcount_bytes(raw: bytes) -> int:
    """Count set bits in ``raw``.

    Prefers the Rust ``fno_math.popcount_bytes`` kernel (popcnt per
    8-byte chunk, ~25× the per-byte Python loop). Falls back to the
    stdlib ``int.bit_count`` when the optional kernel is not built —
    e.g. a plain ``pip install`` without the maturin-built wheel — so
    the CLI + dashboard import cleanly. Both paths are bit-identical.
    """
    try:
        import fno_math
    except ImportError:
        return int.from_bytes(raw, "big").bit_count()
    return int(fno_math.popcount_bytes(raw))


# ────────────────────────────────────────────────────────────────────────
# Sizing constants (locked by 4-agent Reality-check lens, DEC-P0-047)
# ────────────────────────────────────────────────────────────────────────

#: Phase-1 scaffolding default capacity. Covers the 92-chunk first-run
#: bar-key load (~50M keys) with 2× headroom. Phase 3 will retune to the
#: 150M-1B target range per MIGRATION.md #121 + the Renaissance review.
DEFAULT_CAPACITY: Final[int] = 100_000_000

#: Phase-1 default FPR target. At 100M / 1e-7 the bit array is ~419 MB
#: with ~24 hash functions. 1e-7 keeps the 50M-key audit-replay test
#: deterministic at ~5 expected false positives per replay — well within
#: Golden Rule #7 byte-equality tolerance.
DEFAULT_FPR: Final[float] = 1e-7

#: The bit-array ceiling FLOOR: 16 GiB — the original DEC-P0-047 hand-picked
#: value, which left room for indicator buffers + LMDB cache + Python
#: interpreter inside the 48 GiB M4 Pro envelope (CLAUDE.md §8). Since
#: DEC-RAM-BUDGETS-001 the LIVE ceiling :data:`MAX_BLOOM_BYTES` is derived
#: from the machine's detected total RAM; this constant is applied EXACTLY
#: when RAM detection fails (the honest undetected sentinel — the DEC-P0-047
#: behavior, bit-for-bit).
MAX_BLOOM_BYTES_FLOOR: Final[int] = 16 * 1024**3

#: The detected-RAM share the Bloom bit array may claim (DEC-RAM-BUDGETS-001):
#: ``1/3`` gives EXACT parity with the old 16 GiB constant on the CLAUDE.md §8
#: 48 GiB reference box (regression-pinned in
#: ``tests/unit/test_hardware_envelope.py``); a smaller machine derives a
#: smaller wall (portability — the guard refuses BEFORE an allocation that
#: box could never hold), a larger one gains headroom. A PORTABILITY choice,
#: NOT a measured figure (Golden Rule #10).
MAX_BLOOM_BYTES_FRACTION: Final[float] = 1.0 / 3.0

#: Env knob overriding the derived Bloom bit-array ceiling in bytes (``> 0``);
#: validated fail-LOUD on garbage (Golden Rule #1).
MAX_BLOOM_BYTES_ENV: Final[str] = "BRUTEX_MAX_BLOOM_BYTES"


def resolve_max_bloom_bytes(environ: Mapping[str, str] | None = None) -> int:
    """Resolve the Bloom bit-array ceiling (DEC-RAM-BUDGETS-001).

    Delegates to the COMMON :func:`brutex.hardware_envelope.memory_budget_bytes`:
    a set, non-blank ``BRUTEX_MAX_BLOOM_BYTES`` is the validated explicit
    override (a non-integer / non-positive value raises ``ValueError``);
    otherwise the ceiling is ``detected total RAM`` ×
    :data:`MAX_BLOOM_BYTES_FRACTION`, or :data:`MAX_BLOOM_BYTES_FLOOR`
    (16 GiB) EXACTLY when RAM is undetectable. Sizing is results-safe by
    construction — the Bloom is an L1 prefilter over the LMDB exact ground
    truth and never part of run identity; the ceiling changes only WHEN a
    construction refuses (:class:`BloomTooLargeError`), never result bytes.

    Parameters
    ----------
    environ
        Injectable environment mapping (tests); ``None`` reads ``os.environ``.

    Raises
    ------
    ValueError
        If the env value is set and not a positive integer.
    """
    return memory_budget_bytes(
        fraction=MAX_BLOOM_BYTES_FRACTION,
        floor_bytes=MAX_BLOOM_BYTES_FLOOR,
        env=environ,
        env_var=MAX_BLOOM_BYTES_ENV,
    )


#: Hard ceiling on the computed bit-array size — RAM-derived ONCE at import
#: (the once-per-process law; env overrides are read at process start) via
#: :func:`resolve_max_bloom_bytes`. Sweeps that need a larger filter must
#: escalate to sharded / mmap'd / Cuckoo (deferred). 4-agent Hostile lens §3
#: flagged the OOM risk if this guard is absent (DEC-P0-047); the fixed
#: 16 GiB value it locked is now :data:`MAX_BLOOM_BYTES_FLOOR`.
MAX_BLOOM_BYTES: Final[int] = resolve_max_bloom_bytes()

#: V1 minimum FPR floor. Below this, the sizing math implies a bit
#: array that approaches MAX_BLOOM_BYTES for any realistic capacity;
#: sharded follow-up is required. Constructor raises
#: :class:`BloomConfigError` if the caller requests a tighter FPR.
MIN_FPR_TARGET: Final[float] = 1e-9

#: V1 maximum FPR floor (exclusive). FPR ≥ 0.5 makes the filter
#: useless as an L1 fast-fail; rejected at construction.
MAX_FPR_TARGET: Final[float] = 0.5


# ────────────────────────────────────────────────────────────────────────
# Errors
# ────────────────────────────────────────────────────────────────────────


class BloomConfigError(BruteXError):
    """Constructor params violate the V1 sizing envelope.

    Raised when ``capacity`` is non-positive, ``fpr_target`` is outside
    ``[MIN_FPR_TARGET, MAX_FPR_TARGET)``, or the param tuple would
    produce a non-sensical bit array.
    """


class BloomTooLargeError(BloomConfigError):
    """Computed bit array exceeds :data:`MAX_BLOOM_BYTES`.

    Operator action: either tighten ``capacity`` / loosen ``fpr_target``,
    or wait for the sharded / mmap'd follow-up to land.
    """


class BloomCapacityExceeded(BruteXError):
    """:meth:`BloomFilter.add` called past the configured capacity.

    FAIL_CLOSED semantics: silent FPR drift (DEGRADED_MODE) would
    violate the 4-tier dedup correctness contract. The caller's job is
    to (a) record this as a ``BLOOM_CAPACITY_EXCEEDED`` audit entry,
    (b) raise a Telegram alert, (c) halt the affected phase.

    The limit is the constructor's ``capacity`` — an OPERATOR knob, not a
    hard wall (only :data:`MAX_BLOOM_BYTES` is). For the sweep dedup cache
    it is tunable via
    :meth:`brutex.engine.sweep_cache.SweepCache.open`'s ``bloom_capacity``
    (the ``--sweep-cache-bloom-capacity`` flag /
    ``BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY`` env var, R2-SL-F4-F5).

    Reset path: future PR will add a CLI command that drops the filter
    and rebuilds it from the LMDB ground-truth via
    :meth:`WorkItemStore.iter_phase`.
    """


# ────────────────────────────────────────────────────────────────────────
# BloomFilter — wrapper around fastbloom-rs
# ────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class BloomFilter:
    """Thread-safe Bloom filter for content-hash dedup.

    Wraps :class:`fastbloom_rs.BloomFilter` (the published PyPI Rust
    impl) with the BruteX-specific FAIL_CLOSED / sizing-guard /
    audit-emission discipline locked by the DEC-P0-047 4-agent review.

    Parameters
    ----------
    capacity
        Target unique-element count. Bloom math sizes the bit array
        against this number. Insertions past ``capacity`` raise
        :class:`BloomCapacityExceeded`. Defaults to
        :data:`DEFAULT_CAPACITY` (= 100,000,000).
    fpr_target
        Target false-positive rate at full capacity. Must lie in
        ``[MIN_FPR_TARGET, MAX_FPR_TARGET)``. Defaults to
        :data:`DEFAULT_FPR` (= 1e-7).

    Raises
    ------
    BloomConfigError
        On invalid ``capacity`` or ``fpr_target``.
    BloomTooLargeError
        If the computed bit array exceeds :data:`MAX_BLOOM_BYTES`.

    Notes
    -----
    The wrapper deliberately does NOT expose ``fastbloom_rs.BloomFilter``
    methods like ``add_str`` / ``add_int``. Callers must pre-hash to
    ``bytes`` (sha256 today, blake3 per DEC-P0-044 Q-02 tomorrow).
    Hashing inside Bloom would couple to a specific algorithm and
    break the zero-touch migration discipline.
    """

    capacity: int = DEFAULT_CAPACITY
    fpr_target: float = DEFAULT_FPR

    # Computed at construction.
    _m_bits: int = field(init=False)
    _k: int = field(init=False)
    _m_bytes: int = field(init=False)
    _bits_per_item: float = field(init=False)
    _impl: _RustBloomFilter = field(init=False)
    _lock: threading.Lock = field(init=False)
    _insertions_attempted: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        # 1. Validate basic params.
        if self.capacity < 1:
            raise BloomConfigError(
                f"BloomFilter: capacity must be ≥ 1, got {self.capacity}",
            )
        if not (MIN_FPR_TARGET <= self.fpr_target < MAX_FPR_TARGET):
            raise BloomConfigError(
                f"BloomFilter: fpr_target must lie in "
                f"[{MIN_FPR_TARGET}, {MAX_FPR_TARGET}), got {self.fpr_target}. "
                f"Tighter FPR requires sharded / mmap'd follow-up.",
            )

        # 2. Compute Bloom sizing per Bloom (1970) + Mitzenmacher & Upfal
        #    §5.5.3. Both ceil() to avoid silent FPR violation.
        log_p = math.log(self.fpr_target)
        log_2_sq = math.log(2) ** 2
        self._m_bits = math.ceil(-self.capacity * log_p / log_2_sq)
        self._k = max(1, math.ceil((self._m_bits / self.capacity) * math.log(2)))
        self._m_bytes = math.ceil(self._m_bits / 8)
        self._bits_per_item = self._m_bits / self.capacity

        # 3. Hardware envelope guard. Hostile lens §3 CRITICAL.
        if self._m_bytes > MAX_BLOOM_BYTES:
            raise BloomTooLargeError(
                f"BloomFilter: computed bit array {self._m_bytes:_} bytes "
                f"exceeds MAX_BLOOM_BYTES={MAX_BLOOM_BYTES:_}. Tighten "
                f"capacity / loosen fpr_target or wait for sharded "
                f"follow-up.",
            )

        # 4. Initialise the Rust-backed filter. fastbloom-rs picks its
        #    own (m, k) internally from the same (n, p) inputs; we
        #    expose our computed values as properties for observability.
        self._impl = _load_rust_bloom()(self.capacity, self.fpr_target)
        self._lock = threading.Lock()

        # 5. Emit init event. Hostile lens §9: every init must structlog
        #    the achieved config so the audit oracle can replay.
        logger = structlog.get_logger()
        logger.info(
            "bloom_init",
            capacity=self.capacity,
            fpr_target=self.fpr_target,
            m_bits=self._m_bits,
            m_bytes=self._m_bytes,
            k=self._k,
            bits_per_item=self._bits_per_item,
            impl_k=self._impl.hashes(),
            backend="fastbloom-rs",
        )

    # ── Public API ─────────────────────────────────────────────────

    def add(self, content_hash: bytes) -> None:
        """Insert ``content_hash`` into the filter.

        Parameters
        ----------
        content_hash
            Pre-hashed bytes — caller hashes via sha256 (today) or
            blake3 (DEC-P0-044 Q-02 future). The wrapper does NOT
            re-hash internally so the sha256 → blake3 migration is
            zero-touch.

        Raises
        ------
        TypeError
            If ``content_hash`` is not :class:`bytes`.
        BloomCapacityExceeded
            If :attr:`insertions_attempted` has reached :attr:`capacity`.
            Caller MUST catch + emit a ``BLOOM_CAPACITY_EXCEEDED``
            audit entry + halt the affected phase (FAIL_CLOSED).
        """
        if not isinstance(content_hash, bytes):
            raise TypeError(
                f"BloomFilter.add: content_hash must be bytes, got {type(content_hash).__name__}",
            )
        with self._lock:
            if self._insertions_attempted >= self.capacity:
                raise BloomCapacityExceeded(
                    f"BloomFilter capacity {self.capacity:_} reached "
                    f"(insertions_attempted={self._insertions_attempted:_}). "
                    f"Operator must reset + rebuild or escalate "
                    f"to a larger capacity (for the sweep dedup cache: "
                    f"--sweep-cache-bloom-capacity or "
                    f"BRUTEX_SWEEP_CACHE_BLOOM_CAPACITY).",
                )
            self._impl.add_bytes(content_hash)
            self._insertions_attempted += 1

    def might_contain(self, content_hash: bytes) -> bool:
        """Return True if ``content_hash`` is **probably** in the filter.

        Parameters
        ----------
        content_hash
            Pre-hashed bytes. See :meth:`add` for the bytes convention.

        Returns
        -------
        bool
            ``False`` ⇒ ``content_hash`` is **definitely NOT** in the
            filter (the L1 fast-fail guarantee).

            ``True`` ⇒ ``content_hash`` is **probably** in the filter
            with achieved FPR ≤ ``fpr_target`` (per the sizing math) —
            caller falls through to the L2 LMDB ground-truth check.

        Notes
        -----
        **Concurrent false-negative contract** (Hostile lens §6): a
        reader concurrent with its own :meth:`add` of the same
        ``content_hash`` MAY observe a partial bit-set and return
        ``False`` — the read is lockless and the writer's bit-flips
        are not atomic across all ``k`` slots. This is acceptable
        because the L2 LMDB layer is ground-truth: the worst case is
        an extra L2 lookup, never a duplicate fetch reaching the vendor
        API. The 4-tier dedup contract (CLAUDE.md §2 rule 11) tolerates
        this; clients of :class:`BloomFilter` MUST treat ``True`` as
        "maybe" and ``False`` as "very probably not".
        """
        if not isinstance(content_hash, bytes):
            raise TypeError(
                f"BloomFilter.might_contain: content_hash must be bytes, "
                f"got {type(content_hash).__name__}",
            )
        return bool(self._impl.contains(content_hash))

    # ── Observability properties ──────────────────────────────────

    @property
    def insertions_attempted(self) -> int:
        """Total :meth:`add` invocations — the UNIQUE-key count.

        Used as the FAIL_CLOSED capacity gate. Each :meth:`add` still
        increments this counter unconditionally (this class does not
        dedup — a repeated :meth:`add` of the same hash WOULD count
        twice), so the honesty of the gate depends on the caller adding
        each key AT MOST ONCE.

        The production caller (:meth:`brutex.orchestrator.store.WorkItemStore.put`)
        honours that contract: it Bloom-adds a key ONLY on its FIRST
        LMDB insertion, never on a subsequent state-transition put of an
        already-stored key (DEC-DEDUP-CAPACITY-UNIQUE-001). LMDB is the
        ground truth for newness. Before that fix, every combo — put
        multiple times as it moves PENDING→RUNNING→SUCCESS — inflated
        this counter, so the gate could raise
        :class:`BloomCapacityExceeded` at a small FRACTION of ``capacity``
        distinct combos and halt a heavy sweep prematurely. With the fix
        ``insertions_attempted`` equals the number of DISTINCT keys ever
        inserted, so the capacity gate is honest: it trips only when
        ``capacity`` genuinely-unique combos exist. Re-adding the same
        hash 10⁹ times through :meth:`add` directly would still trip the
        gate — dedup is the caller's responsibility, not this filter's.
        """
        with self._lock:
            return self._insertions_attempted

    @property
    def m_bits(self) -> int:
        """Total bits in the underlying bit array (the canonical ``m``)."""
        return self._m_bits

    @property
    def m_bytes(self) -> int:
        """Total bytes in the underlying bit array."""
        return self._m_bytes

    @property
    def k(self) -> int:
        """Number of hash functions per insertion (the canonical ``k``)."""
        return self._k

    @property
    def bits_per_item(self) -> float:
        """``m_bits / capacity`` — useful for sizing audits."""
        return self._bits_per_item

    @property
    def unique_count_estimate(self) -> int:
        """Estimate the unique-element count from bit density.

        Uses the Swamidass + Baldi (2007) estimator:

            n_est = -(m / k) * ln(1 - bits_set / m)

        Recomputed on demand by scanning the entire bit array
        (:math:`O(m_bytes)`). Avoid hot-path callers; intended for
        audit / dashboard / capacity-planning reads.

        DEC-P0-057 (PR-N+4): the bit-density scan is delegated to the
        Rust ``fno_math.popcount_bytes`` kernel (popcnt CPU instruction
        per 8-byte chunk). ~25× faster than the previous per-byte
        Python loop on Phase-1-sized filters. The first attempt to ship
        this 1-liner (DEC-P0-053 / PR-N) was reverted because the
        ``fno_math`` wheel had no bundled type stubs; DEC-P0-055
        (PR-N+2) shipped the stubs and DEC-P0-056 closed the
        un-draft-after-CI-green auto-merge gap, unblocking this PR.

        Returns
        -------
        int
            Estimated unique inserts. Lower-bounded by 0;
            upper-bounded by ``capacity`` (clipped).
        """
        raw = self._impl.get_bytes()
        bits_set = _popcount_bytes(bytes(raw))
        if bits_set == 0:
            return 0
        if bits_set >= self._m_bits:
            # Filter saturated — estimator diverges; return capacity.
            return self.capacity
        try:
            n_est = -(self._m_bits / self._k) * math.log(1 - bits_set / self._m_bits)
        except ValueError:
            return self.capacity
        return min(self.capacity, max(0, math.ceil(n_est)))

    @property
    def empirical_fpr(self) -> float:
        """Estimated false-positive rate at the current insertion count.

        Computed from the unique-element estimate and the canonical
        Bloom FPR formula::

            fpr_est ≈ (1 - exp(-k * n_est / m)) ** k

        Hostile lens §9 mandated this for audit emissions. Like
        :attr:`unique_count_estimate`, recomputed on demand
        (:math:`O(m_bytes)`).
        """
        n_est = self.unique_count_estimate
        if n_est <= 0:
            return 0.0
        return (1.0 - math.exp(-self._k * n_est / self._m_bits)) ** self._k

    # ── Repr ──────────────────────────────────────────────────────

    def __repr__(self) -> str:
        return (
            f"BloomFilter(capacity={self.capacity:_}, "
            f"fpr_target={self.fpr_target}, "
            f"m_bytes={self._m_bytes:_}, k={self._k}, "
            f"inserted={self._insertions_attempted:_})"
        )


# ────────────────────────────────────────────────────────────────────────
# Internal helpers
# ────────────────────────────────────────────────────────────────────────


def _compute_m_bytes(capacity: int, fpr_target: float) -> int:
    """Return the bit array size in bytes for the given sizing.

    Pure helper exposed for tests + the audit oracle. Mirrors the
    arithmetic inside :meth:`BloomFilter.__post_init__`.
    """
    log_p = math.log(fpr_target)
    m_bits = math.ceil(-capacity * log_p / (math.log(2) ** 2))
    return math.ceil(m_bits / 8)


# Suppress an unused-import warning when the type alias is only used by
# tests via the public re-export below.
_ = Any

__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_FPR",
    "MAX_BLOOM_BYTES",
    "MAX_BLOOM_BYTES_ENV",
    "MAX_BLOOM_BYTES_FLOOR",
    "MAX_BLOOM_BYTES_FRACTION",
    "MAX_FPR_TARGET",
    "MIN_FPR_TARGET",
    "BloomCapacityExceeded",
    "BloomConfigError",
    "BloomFilter",
    "BloomTooLargeError",
    "resolve_max_bloom_bytes",
]
