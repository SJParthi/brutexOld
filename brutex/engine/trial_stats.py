"""Trial-statistics sidecar core — the per-tuple (N, V-moments) carrier (ADR-0008 slice 1).

DEC-TRIAL-STATS-CORE-001 (ADR-0008 / DEC-CASCADE-N-DESIGN-LOCK-001 §6 slice 1).
The Stage-4 cascade's Deflated-Sharpe filter must deflate against the
per-(instrument, TF) LEADERBOARD trial count and cross-trial Sharpe variance —
not the entering pool's (`docs/adr/0008-cascade-n-trial-stats-design-lock.md`
§1). The true N exists only at rank time, so each grid tuple (and each Stage-3
refined cell) persists a tiny deterministic JSON **sidecar** beside its Parquet
store carrying exactly the scalars the DSR engine needs, in mergeable moment
form. This module is the PURE core: the data model, the finite-Sharpe moment
accumulation, the exact union merge, and the deterministic atomic JSON IO.
The write POINTS (sweep + Stage-3, ADR-0008 condition 2's
flush → sidecar → mark ordering and the overwrite-iff-``skipped_dedup == 0``
policy), the per-leaderboard builder + manifest freeze (condition 1), and the
cascade consumption (condition 6) are slices 2-4 — this module only provides
the primitives those slices compose, and DOCUMENTS the policy contract its
callers must honor.

Design (the ADR-0008 conditions this module owns):

* **Condition 3 — the N/V population.** ``n_backtested`` counts the combos
  that actually ran a backtest (a Sharpe draw — the
  :func:`brutex.ranking.rank_combos` ``n_trials = len(candidates)`` precedent);
  ``None``/non-finite Sharpes count in N but are EXCLUDED from the moments —
  exactly the :func:`brutex.ranking.overfit.pool_sharpe_dispersion` convention,
  with the SAME shared :data:`~brutex.ranking.overfit._MIN_FINITE_SHARPES`
  threshold (imported, never re-derived). The prune tallies
  (``n_enumerated`` / ``n_conflict_pruned`` / ``n_minhits_dropped``) are audit
  context that make the N definition flippable without re-sweeping.
* **Condition 5 — pairing identity.** Every sidecar carries the tuple's path
  digest and the SHA-256 content hash of the store file it was written beside,
  so drift between a sidecar and its store is detectable, never silent.
* **Condition 2 (the IO half).** :func:`write_tuple_trial_stats` is atomic
  (tempfile + ``os.replace``) and byte-deterministic (sorted keys, repr
  floats, ``allow_nan=False`` — the results-store discipline, reusing its
  atomic writer). The overwrite-iff-``skipped_dedup == 0`` / preserve-on-skips
  policy is the CALLER's obligation (slice 2): merging accumulated scalars is
  FORBIDDEN — no keyed dedup exists for sums, a partial redo would
  double-count — so this module deliberately exposes no sidecar-merge-on-disk
  operation, only whole-file write/read.
* **Conditions 9/12 — one canonical V.** Moments use :func:`math.fsum`
  (correctly rounded, order-independent for a given value multiset);
  :func:`merge_tuple_trial_stats` fsums the part sums — exact over the part
  values, but ULP-distinct in general from one global fsum over the raw
  Sharpes, and the variance identity ``E[x²] − E[x]²`` is ULP-distinct from
  ``statistics.pvariance``'s exact-Fraction path. The engine therefore
  computes V exactly ONE way (from the frozen merged moments, in the
  deterministic builder order — slice 3) and every cross-check against
  ``pvariance`` is APPROXIMATE by design.

Honest envelope (Golden Rule #10): every function here is O(inputs) pure
computation or one small-file IO — a few hundred bytes per tuple; nothing is a
Rule-#5 O(1) hot path and no throughput figure is stated. ``variance`` is the
population variance of the FINITE Sharpes only; with fewer than the shared
minimum finite count it is ``0.0`` and ``deflatable`` is ``False`` (the
PoolDispersion convention). The numerical-cancellation guard clamps a tiny
negative ``E[x²] − E[x]²`` to ``0.0``; residual ULP noise vs a direct
computation is disclosed, not hidden.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from brutex.data.guarded_read import read_regular_text
from brutex.engine.results_store import _atomic_write
from brutex.models import BruteXError
from brutex.ranking.overfit import _MIN_FINITE_SHARPES

#: Sidecar schema version — bump on any incompatible field change.
TRIAL_STATS_SCHEMA_VERSION: Final[int] = 1

#: Sidecar filename suffix, appended to the tuple store's full name
#: (``bull_k3_<digest>.parquet`` → ``bull_k3_<digest>.parquet.trials.json``).
TRIAL_STATS_SUFFIX: Final[str] = ".trials.json"

#: The tuple path digest length (hex chars) — MUST equal the orchestrator's
#: ``_PATH_DIGEST_LEN`` (pinned by test, not imported: this module stays free
#: of the orchestrator's import graph).
_TUPLE_DIGEST_LEN: Final[int] = 16

#: SHA-256 hex length — the store's ``FlushResult.content_hash`` format.
_CONTENT_HASH_LEN: Final[int] = 64

#: The exact payload keys a v1 sidecar must carry (sorted; the JSON contract).
_PAYLOAD_KEYS: Final[tuple[str, ...]] = (
    "n_backtested",
    "n_conflict_pruned",
    "n_enumerated",
    "n_finite",
    "n_minhits_dropped",
    "schema_version",
    "store_content_hash",
    "sum_sharpe",
    "sum_sharpe_sq",
    "tuple_digest",
)


class TrialStatsError(BruteXError):
    """Raised for any trial-stats failure.

    Covers an invalid field (negative count, inconsistent tallies, non-finite
    or impossible moments, a malformed pairing identity), a missing or
    malformed sidecar file, and an unsupported schema version.
    """


def _validate_hex(name: str, value: str, length: int) -> None:
    """Raise :class:`TrialStatsError` unless *value* is ``length`` lowercase hex chars."""
    if len(value) != length:
        raise TrialStatsError(f"{name} must be {length} hex chars, got {len(value)}")
    if value != value.lower():
        raise TrialStatsError(f"{name} must be lowercase hex, got {value!r}")
    try:
        int(value, 16)
    except ValueError as exc:
        raise TrialStatsError(f"{name} {value!r} is not valid hex") from exc


def _validate_moments(n_finite: int, sum_sharpe: float, sum_sharpe_sq: float) -> None:
    """Raise :class:`TrialStatsError` on non-finite or impossible Sharpe moments."""
    for name, value in (("sum_sharpe", sum_sharpe), ("sum_sharpe_sq", sum_sharpe_sq)):
        if not math.isfinite(value):
            raise TrialStatsError(f"{name} must be finite, got {value!r}")
    if sum_sharpe_sq < 0.0:
        raise TrialStatsError(f"sum_sharpe_sq must be >= 0, got {sum_sharpe_sq!r}")
    if n_finite == 0 and (sum_sharpe != 0.0 or sum_sharpe_sq != 0.0):
        raise TrialStatsError(
            f"zero finite Sharpes admit no moments, got "
            f"sum_sharpe={sum_sharpe!r}, sum_sharpe_sq={sum_sharpe_sq!r}"
        )


@dataclass(frozen=True, slots=True)
class TupleTrialStats:
    """One tuple's trial statistics — the sidecar's in-memory form.

    Parameters
    ----------
    n_backtested
        Combos that actually ran a backtest for this tuple (post-conflict,
        post-dedup-novel, post-min-hits) — this tuple's contribution to the
        leaderboard N (ADR-0008 condition 3).
    n_finite
        How many of those produced a finite Sharpe (``<= n_backtested``);
        only these contribute to the moments.
    sum_sharpe, sum_sharpe_sq
        ``math.fsum`` of the finite Sharpes / their squares — the mergeable
        V-moments. Both ``0.0`` when ``n_finite == 0``.
    n_enumerated, n_conflict_pruned, n_minhits_dropped
        The prune tallies (audit context; ``n_conflict_pruned +
        n_minhits_dropped + n_backtested <= n_enumerated`` — the remainder is
        dedup-skipped masks counted by an earlier run).
    tuple_digest
        The tuple's 16-hex path digest (the store filename's collision guard)
        — pairing identity half one (condition 5).
    store_content_hash
        SHA-256 hex of the store file this sidecar was written beside
        (``FlushResult.content_hash``) — pairing identity half two.

    Raises
    ------
    TrialStatsError
        On any negative count, ``n_finite > n_backtested``, inconsistent
        prune tallies, non-finite / impossible moments, or a malformed
        pairing identity.
    """

    n_backtested: int
    n_finite: int
    sum_sharpe: float
    sum_sharpe_sq: float
    n_enumerated: int
    n_conflict_pruned: int
    n_minhits_dropped: int
    tuple_digest: str
    store_content_hash: str

    def __post_init__(self) -> None:
        """Validate every field invariant at construction time."""
        for name in (
            "n_backtested",
            "n_finite",
            "n_enumerated",
            "n_conflict_pruned",
            "n_minhits_dropped",
        ):
            value = getattr(self, name)
            if value < 0:
                raise TrialStatsError(f"{name} must be >= 0, got {value}")
        if self.n_finite > self.n_backtested:
            raise TrialStatsError(
                f"n_finite ({self.n_finite}) cannot exceed n_backtested ({self.n_backtested})"
            )
        accounted = self.n_conflict_pruned + self.n_minhits_dropped + self.n_backtested
        if accounted > self.n_enumerated:
            raise TrialStatsError(
                f"conflict_pruned + minhits_dropped + backtested ({accounted}) "
                f"cannot exceed n_enumerated ({self.n_enumerated})"
            )
        _validate_moments(self.n_finite, self.sum_sharpe, self.sum_sharpe_sq)
        # Canonicalise signed zero (-0.0 → +0.0) so two logically-equal stats
        # always serialize byte-identically (the results-store precedent).
        object.__setattr__(self, "sum_sharpe", self.sum_sharpe + 0.0)
        object.__setattr__(self, "sum_sharpe_sq", self.sum_sharpe_sq + 0.0)
        _validate_hex("tuple_digest", self.tuple_digest, _TUPLE_DIGEST_LEN)
        _validate_hex("store_content_hash", self.store_content_hash, _CONTENT_HASH_LEN)


def tuple_trial_stats_from_sharpes(
    sharpes: Sequence[float | None],
    *,
    n_enumerated: int,
    n_conflict_pruned: int,
    n_minhits_dropped: int,
    tuple_digest: str,
    store_content_hash: str,
) -> TupleTrialStats:
    """Build one tuple's :class:`TupleTrialStats` from its raw per-combo Sharpes.

    *sharpes* is the FULL backtested population (one entry per combo that ran
    ``metrics_for_mask`` — the raw ``float | None`` values, NEVER the
    ``None``→``0.0`` row projection, which would inject fake zeros into V;
    ADR-0008 condition 4). ``None``/non-finite entries count in
    ``n_backtested`` and are excluded from the moments — the
    :func:`~brutex.ranking.overfit.pool_sharpe_dispersion` convention.

    Parameters
    ----------
    sharpes
        Every backtested combo's Sharpe (``float`` or ``None``), any order —
        ``math.fsum`` makes the moments order-independent.
    n_enumerated, n_conflict_pruned, n_minhits_dropped
        The tuple's prune tallies (audit context).
    tuple_digest, store_content_hash
        The pairing identity (condition 5).

    Returns
    -------
    TupleTrialStats
        The validated stats.
    """
    finite = [s for s in sharpes if s is not None and math.isfinite(s)]
    return TupleTrialStats(
        n_backtested=len(sharpes),
        n_finite=len(finite),
        sum_sharpe=math.fsum(finite),
        sum_sharpe_sq=math.fsum(s * s for s in finite),
        n_enumerated=n_enumerated,
        n_conflict_pruned=n_conflict_pruned,
        n_minhits_dropped=n_minhits_dropped,
        tuple_digest=tuple_digest,
        store_content_hash=store_content_hash,
    )


@dataclass(frozen=True, slots=True)
class LeaderboardTrialStats:
    """Merged per-leaderboard trial statistics — the manifest-freeze form.

    Parameters
    ----------
    n_trials
        The leaderboard's total trial count N (Σ ``n_backtested`` across its
        tuples — plus, from slice 3 on, the unique Stage-3 refined rows).
    n_finite
        Total finite-Sharpe trials (the V population size).
    sum_sharpe, sum_sharpe_sq
        The merged V-moments (``math.fsum`` over the part sums).
    complete
        ``False`` when any contributing tuple's sidecar was missing (an old
        tree) — the slice-3 builder's all-or-fallback flag; consumers must
        never deflate against a knowingly partial N (ADR-0008 condition 6).

    Raises
    ------
    TrialStatsError
        On a negative count, ``n_finite > n_trials``, or non-finite /
        impossible moments.
    """

    n_trials: int
    n_finite: int
    sum_sharpe: float
    sum_sharpe_sq: float
    complete: bool = True

    def __post_init__(self) -> None:
        """Validate the merged invariants at construction time."""
        for name in ("n_trials", "n_finite"):
            value = getattr(self, name)
            if value < 0:
                raise TrialStatsError(f"{name} must be >= 0, got {value}")
        if self.n_finite > self.n_trials:
            raise TrialStatsError(
                f"n_finite ({self.n_finite}) cannot exceed n_trials ({self.n_trials})"
            )
        _validate_moments(self.n_finite, self.sum_sharpe, self.sum_sharpe_sq)
        object.__setattr__(self, "sum_sharpe", self.sum_sharpe + 0.0)
        object.__setattr__(self, "sum_sharpe_sq", self.sum_sharpe_sq + 0.0)

    @property
    def variance(self) -> float:
        """Population variance of the finite trial Sharpes — the DSR's V.

        ``0.0`` with fewer than the shared minimum finite count (the
        :func:`~brutex.ranking.overfit.pool_sharpe_dispersion` convention),
        else ``max(0.0, E[x²] − E[x]²)`` from the merged moments — the clamp
        guards floating-point cancellation when the true variance is ~0;
        residual ULP noise vs a direct ``pvariance`` is disclosed, not hidden.
        """
        if self.n_finite < _MIN_FINITE_SHARPES:
            return 0.0
        mean = self.sum_sharpe / self.n_finite
        return max(0.0, self.sum_sharpe_sq / self.n_finite - mean * mean)

    @property
    def deflatable(self) -> bool:
        """``True`` iff the moments support a deflation (≥ 2 finite, V > 0).

        Mirrors :meth:`brutex.ranking.overfit.PoolDispersion.deflatable` —
        a zero-variance or under-populated pool admits no expected-max
        benchmark, and the consumer must fall back loudly (condition 6).
        """
        return self.n_finite >= _MIN_FINITE_SHARPES and self.variance > 0.0


def merge_tuple_trial_stats(
    parts: Iterable[TupleTrialStats], *, complete: bool = True
) -> LeaderboardTrialStats:
    """Merge per-tuple stats into one leaderboard total — exact over the part values.

    Counts add exactly; the moments are ``math.fsum`` over the part sums —
    correctly rounded over those values regardless of part order, but
    ULP-distinct in general from a single global fsum over the raw Sharpes
    (the one-canonical-computation rule, ADR-0008 condition 9: the engine
    freezes and consumes THIS merged form, never a recomputation).

    Parameters
    ----------
    parts
        The leaderboard's per-tuple stats (any count, including none —
        an empty merge is the honest zero: ``n_trials == 0``, undeflatable).
    complete
        The slice-3 builder's all-sidecars-present flag, threaded through.

    Returns
    -------
    LeaderboardTrialStats
        The merged, validated total.
    """
    materialized = list(parts)
    return LeaderboardTrialStats(
        n_trials=sum(p.n_backtested for p in materialized),
        n_finite=sum(p.n_finite for p in materialized),
        sum_sharpe=math.fsum(p.sum_sharpe for p in materialized),
        sum_sharpe_sq=math.fsum(p.sum_sharpe_sq for p in materialized),
        complete=complete,
    )


def trial_stats_sidecar_path(store_path: Path) -> Path:
    """Return the sidecar path for a tuple store — ``<store>.trials.json``.

    The suffix APPENDS to the full store filename (digest included), so the
    sidecar inherits the store path's collision guarantees and the pairing is
    visible in a directory listing.
    """
    return store_path.with_name(store_path.name + TRIAL_STATS_SUFFIX)


def render_tuple_trial_stats(stats: TupleTrialStats) -> bytes:
    """Serialize *stats* to the deterministic sidecar bytes.

    Sorted keys, compact separators, repr floats, ``allow_nan=False`` — the
    same byte-determinism discipline as the results store and the cascade
    manifest, so an unchanged stats object always renders identical bytes
    (Golden Rule #4).
    """
    payload = {
        "schema_version": TRIAL_STATS_SCHEMA_VERSION,
        "n_backtested": stats.n_backtested,
        "n_finite": stats.n_finite,
        "sum_sharpe": stats.sum_sharpe,
        "sum_sharpe_sq": stats.sum_sharpe_sq,
        "n_enumerated": stats.n_enumerated,
        "n_conflict_pruned": stats.n_conflict_pruned,
        "n_minhits_dropped": stats.n_minhits_dropped,
        "tuple_digest": stats.tuple_digest,
        "store_content_hash": stats.store_content_hash,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


def write_tuple_trial_stats(path: Path, stats: TupleTrialStats) -> Path:
    """Atomically write *stats* to *path* (tempfile + ``os.replace``) and return it.

    A kill mid-write never leaves a half-written sidecar (the results-store
    atomic-write discipline, reused). POLICY CONTRACT (ADR-0008 condition 2,
    enforced by the sweep caller, restated here because this is the only
    write surface): a sidecar must only ever be written from the tuple's
    FULL mask population — merging accumulated scalars across partial runs
    is forbidden (a partial redo would double-count; there is no keyed dedup
    for sums). The slice-2 caller enforced this via the
    ``skipped_dedup == 0`` guard; since DEC-MARK-MANY-SENTINEL-001 the
    guarantee is STRUCTURAL — the sentinel-authoritative redo bypasses dedup
    probes on the digest path, so a subset population cannot occur and every
    write is a byte-identical full-population rewrite (a PAIRED sentinel
    short-circuits before any write; an unpaired one forces the full redo).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # The results store's atomic writer is the ONE source of the
    # tempfile+fsync+replace discipline (CLAUDE.md §11 COMMON) — whitebox
    # import, same as the launcher's `_direction_label` precedent.
    _atomic_write(path, render_tuple_trial_stats(stats))
    return path


def read_tuple_trial_stats(path: Path) -> TupleTrialStats:
    """Read + validate a sidecar written by :func:`write_tuple_trial_stats`.

    Raises
    ------
    TrialStatsError
        If *path* does not exist, is not valid JSON, is not a JSON object,
        carries an unsupported ``schema_version``, is missing a field, has a
        wrongly-typed field, or fails any :class:`TupleTrialStats` invariant.
    """
    if not path.exists():
        raise TrialStatsError(f"trial-stats sidecar not found at {path}")
    try:
        payload = json.loads(read_regular_text(path))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TrialStatsError(f"trial-stats sidecar at {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TrialStatsError(f"trial-stats sidecar at {path} must be a JSON object")
    version = payload.get("schema_version")
    if version != TRIAL_STATS_SCHEMA_VERSION:
        raise TrialStatsError(
            f"unsupported trial-stats schema_version {version!r} at {path} "
            f"(expected {TRIAL_STATS_SCHEMA_VERSION})"
        )
    missing = [key for key in _PAYLOAD_KEYS if key not in payload]
    if missing:
        raise TrialStatsError(f"trial-stats sidecar at {path} missing keys: {missing}")
    try:
        return TupleTrialStats(
            n_backtested=int(payload["n_backtested"]),
            n_finite=int(payload["n_finite"]),
            sum_sharpe=float(payload["sum_sharpe"]),
            sum_sharpe_sq=float(payload["sum_sharpe_sq"]),
            n_enumerated=int(payload["n_enumerated"]),
            n_conflict_pruned=int(payload["n_conflict_pruned"]),
            n_minhits_dropped=int(payload["n_minhits_dropped"]),
            tuple_digest=str(payload["tuple_digest"]),
            store_content_hash=str(payload["store_content_hash"]),
        )
    except (TypeError, ValueError) as exc:
        raise TrialStatsError(
            f"trial-stats sidecar at {path} has a malformed field: {exc}"
        ) from exc


__all__ = [
    "TRIAL_STATS_SCHEMA_VERSION",
    "TRIAL_STATS_SUFFIX",
    "LeaderboardTrialStats",
    "TrialStatsError",
    "TupleTrialStats",
    "merge_tuple_trial_stats",
    "read_tuple_trial_stats",
    "render_tuple_trial_stats",
    "trial_stats_sidecar_path",
    "tuple_trial_stats_from_sharpes",
    "write_tuple_trial_stats",
]
