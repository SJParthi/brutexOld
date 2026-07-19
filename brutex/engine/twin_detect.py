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

**RUST-HOSTED LAWS (DEC-RUST-CASCADE-RESULTS-001, slice 2d):** the
fingerprint serialization byte law, the :class:`TwinCandidate` validation
matrix (bool==int puns and CPython ``re``'s raw ``TypeError`` escapes
included), the ``_jaccard`` division law and the two-tier :func:`group_twins`
union-find partition live in ``rust/brutex-cascade/src/twin_detect.rs``,
verified against the FROZEN pre-conversion parity corpus
(``tests/golden/fixtures/cascade_rust_parity/twin_detect.json``). This module
keeps the type face (constants, frozen dataclasses, the error class) plus the
boundary marshalling: datetimes cross as exact epoch microseconds through the
ONE facade law (:func:`brutex.engine.cascade_rust.epoch_us`) and the BLAKE3
digest itself runs HERE over the crate-built serialization bytes (the crate
is pinned pyo3-only — the recorded slice-2d blake3 split). The pre-conversion
wheel-free posture is therefore CHANGED by design: the detector now
hard-requires the brutex-cascade wheel through the ``BRUTEX_CASCADE_RUST``
facade (absent wheel = a loud typed refusal naming the bootstrap remediation,
never a silent degrade).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, cast

import blake3

from brutex.engine.cascade_rust import epoch_us, require_cascade_rust
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
#: never silently collide. Threaded into the Rust serializer as data —
#: the single source stays HERE (the slice-2c ``SORT_ALIASES`` precedent).
FINGERPRINT_VERSION_PREFIX: Final[str] = "bxtwin1|"

#: The pinned tier-2 near-identity threshold (R34 / design A.6): two
#: candidates whose entry-timestamp sets have Jaccard similarity at or above
#: this value are behavioral twins. Boundary INCLUSIVE (``>=``), test-pinned.
TWIN_JACCARD_THRESHOLD: Final[float] = 0.90


class TwinDetectError(BruteXError):
    """Raised on invalid twin-detector inputs (junk candidates / thresholds)."""


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

    RUST-HOSTED serialization (DEC-RUST-CASCADE-RESULTS-001): the crate
    builds the canonical bytes; the BLAKE3 digest runs here (the blake3
    split). A NAIVE timestamp escapes the µs marshalling as the raw
    ``TypeError`` the deleted law's datetime subtraction raised — per trade,
    in field order (entry before exit).
    """
    rows: list[tuple[int, int, str, int]] = [
        (
            epoch_us(trade.entry_ts),
            epoch_us(trade.exit_ts),
            trade.direction.value,
            trade.pnl_paisa,
        )
        for trade in trades
    ]
    serialized = cast(
        "str",
        require_cascade_rust().twin_serialize(
            FINGERPRINT_VERSION_PREFIX,
            [row[0] for row in rows],
            [row[1] for row in rows],
            [row[2] for row in rows],
            [row[3] for row in rows],
        ),
    )
    # str(...) keeps this typed `str` even where the blake3 wheel (and its
    # stubs) is absent and the import degrades to Any — e.g. the isolated
    # pre-commit mypy env (the stage3_wire / #575 combo.py precedent).
    return str(blake3.blake3(serialized.encode("utf-8")).hexdigest())


def entry_ts_us_set(trades: Sequence[CapturedTrade]) -> frozenset[int]:
    """Return the trade list's ENTRY timestamps as a UTC-epoch-µs frozenset.

    The tier-2 similarity signature: entry instants say WHEN the behavior
    fires, which is what near-twin detection compares (a shared-entry pair
    whose exits differ slightly is still one behavior family candidate).
    Duplicate entry instants collapse (set semantics). ``O(trades)``.
    Pure boundary marshalling — the µs conversion IS the facade's ONE
    timestamp law (:func:`~brutex.engine.cascade_rust.epoch_us`) and the
    frozenset is the Python-facing container type; no crate hop.
    """
    return frozenset(epoch_us(trade.entry_ts) for trade in trades)


@dataclass(frozen=True, slots=True)
class TwinCandidate:
    """One pooled winner offered to :func:`group_twins` (design A.6 shape).

    ``run_id`` is the store's per-combo row key; ``mask_bits`` the combo's
    condition mask (its ``bit_count()`` is the simplicity rank);
    ``fingerprint`` the tier-1 digest; ``entry_ts_us`` the tier-2 signature.
    Build via :meth:`from_trades` to keep fingerprint and signature provably
    consistent with ONE trade list. Validation is RUST-HOSTED
    (DEC-RUST-CASCADE-RESULTS-001) — the raised refusals are the REAL
    :class:`TwinDetectError` with byte-identical messages, and a non-string
    fingerprint escapes as CPython ``re``'s own raw ``TypeError``.
    """

    run_id: str
    mask_bits: int
    fingerprint: str
    entry_ts_us: frozenset[int]

    def __post_init__(self) -> None:
        require_cascade_rust().twin_validate_candidate(
            self.run_id, self.mask_bits, self.fingerprint, TwinDetectError
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


def _jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """Jaccard similarity of two entry-timestamp sets (RUST-HOSTED).

    Two EMPTY sets are defined identical (1.0): two zero-trade behaviors are
    the same behavior — reachable only via hand-built candidates, since
    zero-trade lists already share the empty-list fingerprint at tier 1.
    """
    return cast("float", require_cascade_rust().twin_jaccard(a, b))


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
    sorted ``run_id`` order (property-pinned). RUST-HOSTED
    (DEC-RUST-CASCADE-RESULTS-001): the crate returns INDICES into the pool,
    so the mappings below carry the caller's ORIGINAL ``run_id`` objects.

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
    pool = list(candidates)
    family_pairs, members = cast(
        "tuple[list[tuple[int, int]], list[tuple[int, list[int]]]]",
        require_cascade_rust().twin_group(pool, jaccard_threshold, TwinDetectError),
    )
    family_by_run_id: dict[str, str] = {
        pool[member].run_id: pool[rep].run_id for member, rep in family_pairs
    }
    members_by_family: dict[str, tuple[str, ...]] = {
        pool[rep].run_id: tuple(pool[member].run_id for member in member_ids)
        for rep, member_ids in members
    }
    return TwinFamilies(
        family_by_run_id=family_by_run_id,
        members_by_family=members_by_family,
    )
