"""Behavioral twin detector — trade-list fingerprint families (R34).

DEC-SWEEP-RESULTS-DEPTH-001 (WP4). Mask-uniqueness is NOT behavior-uniqueness:
two different condition masks can trade IDENTICALLY over the swept window when
one condition subsumes another. This module groups stored winners into
behavioral FAMILIES so the R21/R26 global top-100 shows 100 genuinely
different behaviors, with the SIMPLEST member of each family (fewest mask
bits) as its representative and every member visible on drill-down.

## The two tiers

* **Tier 1 — exact fingerprint.** :func:`trade_fingerprint` is the BLAKE3 hex
  digest of the versioned canonical trade-list serialization
  ``"bxtwin1|" + "\\n".join(f"{entry_ts_us}|{exit_ts_us}|{direction}|{pnl_paisa}")``
  in CHRONOLOGICAL order (the walker's natural output order — never
  re-sorted). Only the four behavior-identity fields participate: entry/exit
  timestamps (UTC epoch microseconds — exact ints, no floats), direction and
  signed P&L. Every separator (``|``, ``\\n``) is unreachable from the integer
  and ``BUY``/``SELL`` field alphabets, so the serialization is injective —
  equal fingerprints ⇔ equal identity projections (up to hash collision).
  The ``bxtwin1`` version prefix lets a future v2 change the law without a
  silent cross-version collision.
* **Tier 2 — near-identity.** :func:`group_twins` runs a deterministic
  union-find over the candidate pool, merging every pair whose ENTRY-timestamp
  sets have Jaccard similarity ``>= jaccard_threshold`` (the pinned
  :data:`TWIN_JACCARD_THRESHOLD` = 0.90 by default, boundary INCLUSIVE).
  Union-find makes near-identity transitive by construction (A~B and B~C group
  A,C even when the A↔C pair alone sits below the threshold) — one behavior
  family, not a clique requirement.

## Determinism law (stable ``family_id`` across re-runs)

The grouping is a pure function of the candidate SET: candidates are
processed in sorted ``run_id`` order and tier-2 pairs are enumerated in sorted
``(run_id_a, run_id_b)`` order, so the result is input-order-independent
(property-pinned); the family partition itself is the connected components of
a fixed similarity graph, which no enumeration order can change. The family
representative is the member with the FEWEST mask bits
(``mask_bits.bit_count()``), ties broken ``run_id`` ASC; ``family_id`` is the
representative's ``run_id`` — stable across rebuilds given the same sources.

## Cross-bucket twins — by construction

Candidates carry no bucket coordinates: fingerprints are computed per combo
(and persisted in every tuple's aggs sidecar by WP2/WP5), and grouping runs
ONCE at the GLOBAL top-100 build over the pooled candidates of ALL
(tf × direction × arm) buckets — so a twin pair straddling two buckets groups
exactly like an intra-bucket pair, with zero special-casing.

## Honest envelope (Golden Rule #10)

* :func:`trade_fingerprint` is ``O(trades)`` per combo, computed ONCE at
  run-end; thereafter the stored fingerprint is an O(1) lookup key (the R34
  claim — the LOOKUP is O(1), never the first computation).
* :func:`group_twins` is ``O(C^2)`` pair enumeration over the candidate pool
  (each pair paying an ``O(|entry sets|)`` Jaccard) — NOT O(1) and NOT
  claimed to be; the pool is BOUNDED by design (the top-100 build's gate-passer
  candidates), and pairs already connected are skipped without a set
  operation. Pure and deterministic: no I/O, no globals, no wall-clock.

This module is deliberately wheel-free and import-light (stdlib + ``blake3``
+ :class:`~brutex.models.BruteXError` only); the
:class:`~brutex.engine.trade_capture.CapturedTrade` dependency is
type-checking-only, so the detector runs on a box with no Rust wheels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

import blake3

from brutex.models import BruteXError

if TYPE_CHECKING:  # pragma: no cover - import only for static typing
    from collections.abc import Mapping, Sequence

    from brutex.engine.trade_capture import CapturedTrade

__all__ = [
    "FINGERPRINT_VERSION_PREFIX",
    "TWIN_JACCARD_THRESHOLD",
    "TwinCandidate",
    "TwinDetectError",
    "TwinFamilies",
    "entry_ts_us_set",
    "group_twins",
    "trade_fingerprint",
]

#: The versioned fingerprint-serialization prefix (design B6). A future
#: serialization change bumps this to ``bxtwin2|`` so v1 and v2 digests can
#: never silently collide.
FINGERPRINT_VERSION_PREFIX: Final[str] = "bxtwin1|"

#: The pinned tier-2 near-identity threshold (R34 / design A.6): two
#: candidates whose entry-timestamp sets have Jaccard similarity at or above
#: this value are behavioral twins. Boundary INCLUSIVE (``>=``), test-pinned.
TWIN_JACCARD_THRESHOLD: Final[float] = 0.90

#: A fingerprint is exactly one lowercase 64-hex BLAKE3 digest — anything else
#: in a candidate is junk data, refused loudly (Golden Rule #1).
_FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")

#: Valid ``jaccard_threshold`` half-open envelope ``(0.0, 1.0]``: at 0 or
#: below every disjoint pair would merge (nonsense); above 1 nothing can.
_THRESHOLD_MIN_EXCLUSIVE: Final[float] = 0.0
_THRESHOLD_MAX_INCLUSIVE: Final[float] = 1.0

#: Unix epoch (UTC) — the exact-integer microsecond timestamp anchor
#: (mirrors the private ``trade_store`` helper; re-implemented here so this
#: leaf never imports the polars-carrying sidecar writer — design A.5 note).
_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

_ONE_US: Final[timedelta] = timedelta(microseconds=1)


class TwinDetectError(BruteXError):
    """Raised on invalid twin-detector inputs (junk candidates / thresholds)."""


def _ts_to_us(ts: datetime) -> int:
    """Convert a tz-aware datetime to exact UTC epoch microseconds (no floats)."""
    return (ts - _EPOCH_UTC) // _ONE_US


def trade_fingerprint(trades: Sequence[CapturedTrade]) -> str:
    """Return the tier-1 behavioral fingerprint of *trades* (64-hex BLAKE3).

    Canonical serialization (design B6): the :data:`FINGERPRINT_VERSION_PREFIX`
    followed by one ``entry_ts_us|exit_ts_us|direction|pnl_paisa`` line per
    trade, newline-joined, in the GIVEN order (chronological — the walker's
    natural output order; the caller never re-sorts). Only the four
    behavior-identity fields participate: excursions, prices, exit reason,
    VIX stamps and 1m-path fields are deliberately NOT part of behavior
    identity. An empty trade list hashes the bare prefix — deterministic,
    shared by every zero-trade combo. ``O(trades)`` time.
    """
    body = "\n".join(
        f"{_ts_to_us(trade.entry_ts)}|{_ts_to_us(trade.exit_ts)}"
        f"|{trade.direction.value}|{trade.pnl_paisa}"
        for trade in trades
    )
    # str(...) keeps this typed `str` even where the blake3 wheel (and its
    # stubs) is absent and the import degrades to Any — e.g. the isolated
    # pre-commit mypy env (the stage3_wire / #575 combo.py precedent).
    return str(blake3.blake3((FINGERPRINT_VERSION_PREFIX + body).encode("utf-8")).hexdigest())


def entry_ts_us_set(trades: Sequence[CapturedTrade]) -> frozenset[int]:
    """Return the trade list's ENTRY timestamps as a UTC-epoch-µs frozenset.

    The tier-2 similarity signature: entry instants say WHEN the behavior
    fires, which is what near-twin detection compares (a shared-entry pair
    whose exits differ slightly is still one behavior family candidate).
    Duplicate entry instants collapse (set semantics). ``O(trades)``.
    """
    return frozenset(_ts_to_us(trade.entry_ts) for trade in trades)


@dataclass(frozen=True, slots=True)
class TwinCandidate:
    """One pooled winner offered to :func:`group_twins` (design A.6 shape).

    ``run_id`` is the store's per-combo row key; ``mask_bits`` the combo's
    condition mask (its ``bit_count()`` is the simplicity rank);
    ``fingerprint`` the tier-1 digest; ``entry_ts_us`` the tier-2 signature.
    Build via :meth:`from_trades` to keep fingerprint and signature provably
    consistent with ONE trade list.
    """

    run_id: str
    mask_bits: int
    fingerprint: str
    entry_ts_us: frozenset[int]

    def __post_init__(self) -> None:
        if not self.run_id:
            raise TwinDetectError("TwinCandidate.run_id must be non-empty")
        if self.mask_bits < 0:
            raise TwinDetectError(
                f"TwinCandidate.mask_bits must be >= 0, got {self.mask_bits} "
                f"(run_id {self.run_id!r})"
            )
        if _FINGERPRINT_RE.fullmatch(self.fingerprint) is None:
            raise TwinDetectError(
                "TwinCandidate.fingerprint must be a 64-char lowercase hex digest, "
                f"got {self.fingerprint!r} (run_id {self.run_id!r})"
            )

    @classmethod
    def from_trades(
        cls, *, run_id: str, mask_bits: int, trades: Sequence[CapturedTrade]
    ) -> TwinCandidate:
        """Build a candidate whose fingerprint + entry signature share one source.

        The safe constructor: :func:`trade_fingerprint` and
        :func:`entry_ts_us_set` are derived from the SAME *trades*, so the two
        tiers can never silently disagree about which list they describe.
        """
        return cls(
            run_id=run_id,
            mask_bits=mask_bits,
            fingerprint=trade_fingerprint(trades),
            entry_ts_us=entry_ts_us_set(trades),
        )


@dataclass(frozen=True, slots=True)
class TwinFamilies:
    """The family partition produced by :func:`group_twins`.

    ``family_by_run_id`` maps EVERY pooled candidate (singletons included) to
    its ``family_id`` = the representative's ``run_id`` (fewest mask bits,
    tie → ``run_id`` ASC). ``members_by_family`` lists each family's members
    ``run_id`` ASC; its keys are inserted in ``family_id`` ASC order so
    iteration is deterministic. The two mappings describe one partition:
    every run_id appears in exactly one members tuple.
    """

    family_by_run_id: Mapping[str, str]
    members_by_family: Mapping[str, tuple[str, ...]]


def _find(parent: dict[str, str], run_id: str) -> str:
    """Union-find root lookup with two-pass path compression. Amortized ~O(1)."""
    root = run_id
    while parent[root] != root:
        root = parent[root]
    while parent[run_id] != root:
        parent[run_id], run_id = root, parent[run_id]
    return root


def _union(parent: dict[str, str], run_id_a: str, run_id_b: str) -> None:
    """Merge two components, keeping the lexicographically smaller root.

    Union-by-min-run_id makes the internal tree shape (and hence every
    intermediate ``parent`` state) a pure function of the edge SET — one more
    determinism belt on top of the sorted enumeration order. Callers only
    union distinct components (tier 1 unions each fresh run_id once; tier 2
    skips already-connected pairs), so no equal-roots branch exists here.
    """
    root_a = _find(parent, run_id_a)
    root_b = _find(parent, run_id_b)
    if root_b < root_a:
        root_a, root_b = root_b, root_a
    parent[root_b] = root_a


def _jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """Jaccard similarity of two entry-timestamp sets.

    Two EMPTY sets are defined identical (1.0): two zero-trade behaviors are
    the same behavior — reachable only via hand-built candidates, since
    zero-trade lists already share the empty-list fingerprint at tier 1.
    """
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def group_twins(
    candidates: Sequence[TwinCandidate],
    *,
    jaccard_threshold: float = TWIN_JACCARD_THRESHOLD,
) -> TwinFamilies:
    """Partition *candidates* into behavioral twin families (R34).

    Tier 1 merges identical fingerprints; tier 2 merges every pair whose
    entry-timestamp sets have Jaccard similarity ``>= jaccard_threshold``
    (boundary inclusive), with union-find transitivity. Deterministic and
    input-order-independent: candidates are processed and pairs enumerated in
    sorted ``run_id`` order (property-pinned).

    Parameters
    ----------
    candidates
        The pooled winners (all buckets — cross-bucket twins group by
        construction). ``run_id`` values must be unique.
    jaccard_threshold
        Tier-2 similarity floor, in ``(0.0, 1.0]``. The default is the pinned
        :data:`TWIN_JACCARD_THRESHOLD`.

    Returns
    -------
    TwinFamilies
        The full partition; a candidate with no twin is its own family
        (``family_id`` = its own ``run_id``).

    Raises
    ------
    TwinDetectError
        On a duplicate ``run_id`` in the pool, or a *jaccard_threshold*
        outside ``(0.0, 1.0]`` (NaN included) — junk inputs fail loudly,
        never a silently-wrong grouping.
    """
    if not _THRESHOLD_MIN_EXCLUSIVE < jaccard_threshold <= _THRESHOLD_MAX_INCLUSIVE:
        raise TwinDetectError(
            f"group_twins: jaccard_threshold must be in (0.0, 1.0], got {jaccard_threshold!r}"
        )
    by_run_id: dict[str, TwinCandidate] = {}
    for candidate in candidates:
        if candidate.run_id in by_run_id:
            raise TwinDetectError(
                f"group_twins: duplicate run_id {candidate.run_id!r} in the candidate pool"
            )
        by_run_id[candidate.run_id] = candidate

    ordered_ids = sorted(by_run_id)
    parent: dict[str, str] = {run_id: run_id for run_id in ordered_ids}

    # Tier 1 — identical fingerprints: union each run_id into the FIRST
    # (sorted-order) holder of its fingerprint.
    anchor_by_fingerprint: dict[str, str] = {}
    for run_id in ordered_ids:
        anchor = anchor_by_fingerprint.setdefault(by_run_id[run_id].fingerprint, run_id)
        if anchor != run_id:
            _union(parent, anchor, run_id)

    # Tier 2 — near-identical entry sets: pairs in sorted (run_id_a, run_id_b)
    # order; already-connected pairs skip the set arithmetic (a redundant
    # union could never change the components anyway).
    for i, run_id_a in enumerate(ordered_ids):
        set_a = by_run_id[run_id_a].entry_ts_us
        for run_id_b in ordered_ids[i + 1 :]:
            if _find(parent, run_id_a) == _find(parent, run_id_b):
                continue
            if _jaccard(set_a, by_run_id[run_id_b].entry_ts_us) >= jaccard_threshold:
                _union(parent, run_id_a, run_id_b)

    members_by_root: dict[str, list[str]] = {}
    for run_id in ordered_ids:
        members_by_root.setdefault(_find(parent, run_id), []).append(run_id)

    representative_by_root = {
        root: min(
            members,
            key=lambda run_id: (by_run_id[run_id].mask_bits.bit_count(), run_id),
        )
        for root, members in members_by_root.items()
    }
    family_by_run_id = {
        run_id: representative_by_root[_find(parent, run_id)] for run_id in ordered_ids
    }
    members_by_family = {
        representative_by_root[root]: tuple(members)
        for root, members in sorted(
            members_by_root.items(), key=lambda item: representative_by_root[item[0]]
        )
    }
    return TwinFamilies(
        family_by_run_id=family_by_run_id,
        members_by_family=members_by_family,
    )
