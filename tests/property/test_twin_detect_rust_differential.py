"""Hypothesis differentials for the Rust-hosted twin_detect laws.

DEC-RUST-CASCADE-RESULTS-001 phase C (slice 2d). The converted Python law
bodies are DELETED, so each property here drives the Rust-backed shim
(``brutex.engine.twin_detect``) against a VERBATIM in-test copy of the
pre-conversion Python law frozen from commit 319ccdf87 (the retained
independent-oracle-in-test convention — the slice-2a/2b/2c
``test_*_rust_differential.py`` precedent, R45): hypothesis mutates valid
inputs with hostile edits, BOTH sides run to an OUTCOME, and the outcomes
must be EXACTLY equal — the value on success, the SAME exception TYPE and
FULL ``str`` on failure (the raw ``TypeError`` escapes — a naive
timestamp's datetime subtraction, CPython ``re``'s non-string patterns —
ARE the frozen law, so ``Exception`` is caught on both sides).

Priority targets (the S2D Phase-B divergence-risk roster):

* the fingerprint-bytes law over hostile trade lists (negative/zero pnl,
  duplicate timestamps, pre-epoch and year-9999 extremes, exit-before-entry,
  the naive-ts raw ``TypeError`` escape parity in per-trade field order);
* the ``TwinCandidate`` validation matrix over hostile field mixes (bool
  puns, non-int masks, non-string fingerprints) INCLUDING multi-defect
  construction ORDER (run_id → mask → fingerprint);
* the jaccard 0.90-INCLUSIVE boundary via CONSTRUCTED subset/union shapes
  whose ratio is the exact threshold double (merge at the boundary, split
  one ULP above);
* the threshold envelope over hostile objects (``True`` ≡ 1.0; ``-0.0`` /
  ``nan`` / ``inf`` refusals with exception type + message parity; the
  non-numeric raw ``TypeError`` from the chained compare);
* ``group_twins`` over random pools — family partition + representative +
  ``family_id`` equality INCLUDING the two mappings' insertion ORDER, the
  input-order-independence law (the oracle run on the SAME shuffled order),
  mask-bit ties, and the str-subclass run_id identity law.

Disclosed divergence envelope (scoped OUT of the equality strategies and
pinned separately in ``TestDisclosedEnvelope``, each asserted BOTH ways so
a future closure flips the pin loudly):

* a lone-surrogate ``run_id`` fails UTF-8 extraction (``UnicodeEncodeError``)
  inside ``group_twins`` where the deleted law grouped it — production
  run_ids are BLAKE3 hex;
* an ``entry_ts_us`` member beyond ±i64 fails extraction
  (``OverflowError``) where the deleted law compared unbounded ints — the
  epoch-µs domain is ±2.5e17 by the datetime years-1..9999 law;
* an exact-numeric threshold (``Fraction``, ``Decimal`` — any
  exact-numeric type with ``__float__``) passes the rich-compare envelope
  then extracts via ``__float__`` (the crate compares against the rounded
  double) where the deleted law's tier-2 ``>=`` compared exactly — an
  undocumented input class, corpus-free; pinned BOTH ways for Fraction
  AND Decimal.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Final, cast

import blake3
import pytest
from brutex.engine.twin_detect import (
    FINGERPRINT_VERSION_PREFIX,
    TwinCandidate,
    TwinDetectError,
    TwinFamilies,
    entry_ts_us_set,
    group_twins,
    trade_fingerprint,
)
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes.wp4_twin_fixtures import fingerprint_seed, make_captured_trade

# ───────────────────────────── outcome helper ────────────────────────────────


def _outcome(fn: Callable[[], object]) -> tuple[object, ...]:
    """Run *fn* to an outcome: ``("ok", value)`` or ``("exc", type, str)``.

    Raw-escape parity (the naive-ts
    ``TypeError``, CPython ``re``'s pattern ``TypeError``) IS the property;
    ``Exception`` (narrowed from ``BaseException`` at the 2026-07-19 CodeQL
    round) still captures every frozen raw escape — all are ordinary
    ``Exception`` subclasses — while ``KeyboardInterrupt``/``SystemExit``
    and a pyo3 ``PanicException`` now propagate LOUDLY instead of being
    laundered into a comparable outcome.
    """
    try:
        return ("ok", fn())
    except Exception as exc:  # outcome parity is the property
        return ("exc", type(exc), str(exc))


# ───────────────── VERBATIM pre-conversion oracles (319ccdf87) ───────────────

_ORACLE_EPOCH_UTC: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_ORACLE_ONE_US: Final[timedelta] = timedelta(microseconds=1)


def _oracle_ts_to_us(ts: datetime) -> int:
    """VERBATIM 319ccdf87 ``_ts_to_us``."""
    return (ts - _ORACLE_EPOCH_UTC) // _ORACLE_ONE_US


def _oracle_trade_fingerprint(trades: Sequence[object]) -> str:
    """VERBATIM 319ccdf87 ``trade_fingerprint`` (attribute duck-typing)."""
    body = "\n".join(
        f"{_oracle_ts_to_us(trade.entry_ts)}|{_oracle_ts_to_us(trade.exit_ts)}"  # type: ignore[attr-defined]
        f"|{trade.direction.value}|{trade.pnl_paisa}"  # type: ignore[attr-defined]
        for trade in trades
    )
    return str(blake3.blake3((FINGERPRINT_VERSION_PREFIX + body).encode("utf-8")).hexdigest())


def _oracle_entry_ts_us_set(trades: Sequence[object]) -> frozenset[int]:
    """VERBATIM 319ccdf87 ``entry_ts_us_set``."""
    return frozenset(_oracle_ts_to_us(trade.entry_ts) for trade in trades)  # type: ignore[attr-defined]


_ORACLE_FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{64}")


def _oracle_validate_candidate(run_id: object, mask_bits: object, fingerprint: object) -> None:
    """VERBATIM 319ccdf87 ``TwinCandidate.__post_init__`` (self.* → params)."""
    if not run_id:
        raise TwinDetectError("TwinCandidate.run_id must be non-empty")
    if mask_bits < 0:  # type: ignore[operator]
        raise TwinDetectError(
            f"TwinCandidate.mask_bits must be >= 0, got {mask_bits} (run_id {run_id!r})"
        )
    if _ORACLE_FINGERPRINT_RE.fullmatch(fingerprint) is None:  # type: ignore[call-overload]
        raise TwinDetectError(
            "TwinCandidate.fingerprint must be a 64-char lowercase hex digest, "
            f"got {fingerprint!r} (run_id {run_id!r})"
        )


def _oracle_find(parent: dict[str, str], run_id: str) -> str:
    """VERBATIM 319ccdf87 ``_find``."""
    root = run_id
    while parent[root] != root:
        root = parent[root]
    while parent[run_id] != root:
        parent[run_id], run_id = root, parent[run_id]
    return root


def _oracle_union(parent: dict[str, str], run_id_a: str, run_id_b: str) -> None:
    """VERBATIM 319ccdf87 ``_union``."""
    root_a = _oracle_find(parent, run_id_a)
    root_b = _oracle_find(parent, run_id_b)
    if root_b < root_a:
        root_a, root_b = root_b, root_a
    parent[root_b] = root_a


def _oracle_jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    """VERBATIM 319ccdf87 ``_jaccard``."""
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b)


def _oracle_group_twins(
    candidates: Sequence[TwinCandidate], *, jaccard_threshold: object
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """VERBATIM 319ccdf87 ``group_twins`` body.

    The one elision: the tail returns the two mappings directly instead of
    wrapping them in ``TwinFamilies`` — the container has NO validation, so
    the returned dicts (values AND insertion order) ARE the frozen law.
    """
    if not 0.0 < jaccard_threshold <= 1.0:  # type: ignore[operator]
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

    anchor_by_fingerprint: dict[str, str] = {}
    for run_id in ordered_ids:
        anchor = anchor_by_fingerprint.setdefault(by_run_id[run_id].fingerprint, run_id)
        if anchor != run_id:
            _oracle_union(parent, anchor, run_id)

    for i, run_id_a in enumerate(ordered_ids):
        set_a = by_run_id[run_id_a].entry_ts_us
        for run_id_b in ordered_ids[i + 1 :]:
            if _oracle_find(parent, run_id_a) == _oracle_find(parent, run_id_b):
                continue
            if _oracle_jaccard(set_a, by_run_id[run_id_b].entry_ts_us) >= jaccard_threshold:  # type: ignore[operator]
                _oracle_union(parent, run_id_a, run_id_b)

    members_by_root: dict[str, list[str]] = {}
    for run_id in ordered_ids:
        members_by_root.setdefault(_oracle_find(parent, run_id), []).append(run_id)

    representative_by_root = {
        root: min(
            members,
            key=lambda run_id: (by_run_id[run_id].mask_bits.bit_count(), run_id),
        )
        for root, members in members_by_root.items()
    }
    family_by_run_id = {
        run_id: representative_by_root[_oracle_find(parent, run_id)] for run_id in ordered_ids
    }
    members_by_family = {
        representative_by_root[root]: tuple(members)
        for root, members in sorted(
            members_by_root.items(), key=lambda item: representative_by_root[item[0]]
        )
    }
    return family_by_run_id, members_by_family


# ───────────────────────────── shared helpers ────────────────────────────────


def _assert_group_parity(pool: Sequence[TwinCandidate], threshold: object) -> None:
    """Whole-call parity: partition (values + ORDER) or exception type+str."""
    oracle = _outcome(lambda: _oracle_group_twins(pool, jaccard_threshold=threshold))
    live = _outcome(lambda: group_twins(pool, jaccard_threshold=threshold))  # type: ignore[arg-type]
    if oracle[0] == "exc":
        assert live == oracle
        return
    assert live[0] == "ok", f"live diverged: {live!r} vs oracle ok"
    fam, members = cast("tuple[dict[str, str], dict[str, tuple[str, ...]]]", oracle[1])
    families = cast(TwinFamilies, live[1])
    assert list(families.family_by_run_id.items()) == list(fam.items())
    assert list(families.members_by_family.items()) == list(members.items())


_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)

#: Epoch-µs domain that maps onto valid tz-aware datetimes (year 1..9999).
_US = st.integers(min_value=-62_000_000_000_000_000, max_value=250_000_000_000_000_000)

#: int64 pnl (the CapturedTrade contract; beyond-i64 is the disclosed envelope).
_PNL = st.integers(min_value=-(2**63) + 1, max_value=2**63 - 1)

_SIDES = st.sampled_from([Side.BUY, Side.SELL])


def _trade_from_row(entry_us: int, exit_us: int, direction: Side, pnl: int) -> object:
    return make_captured_trade(
        entry_ts=_EPOCH + timedelta(microseconds=entry_us),
        exit_ts=_EPOCH + timedelta(microseconds=exit_us),
        direction=direction,
        pnl_paisa=pnl,
    )


@st.composite
def _trade_lists(draw: st.DrawFn) -> list[object]:
    rows = draw(st.lists(st.tuples(_US, _US, _SIDES, _PNL), max_size=6))
    return [_trade_from_row(e, x, d, p) for e, x, d, p in rows]


@st.composite
def _pools(draw: st.DrawFn) -> list[TwinCandidate]:
    """Random VALID candidate pools engineered for tier-1/tier-2 collisions."""
    run_ids = draw(st.lists(st.text(min_size=1, max_size=6), max_size=6, unique=True))
    pool: list[TwinCandidate] = []
    for run_id in run_ids:
        pool.append(
            TwinCandidate(
                run_id=run_id,
                mask_bits=draw(st.one_of(st.booleans(), st.integers(0, 2**70))),
                # A 4-seed alphabet forces frequent tier-1 fingerprint hits.
                fingerprint=fingerprint_seed(draw(st.integers(0, 3))),
                # A 13-int universe forces frequent tier-2 overlaps.
                entry_ts_us=draw(st.frozensets(st.integers(0, 12), max_size=10)),
            )
        )
    return pool


#: VALID thresholds for the partition-equality properties.
_VALID_THRESHOLDS = st.one_of(
    st.sampled_from([0.5, 0.9, 1.0, True, 1]),
    st.floats(min_value=0.05, max_value=1.0, allow_nan=False),
)


# ─────────────────────────── fingerprint family ──────────────────────────────


class TestFingerprintDifferential:
    """The bxtwin1 serialization + digest law vs the verbatim oracle."""

    @given(trades=_trade_lists())
    def test_fingerprint_matches_the_deleted_law(self, trades: list[object]) -> None:
        assert trade_fingerprint(trades) == _oracle_trade_fingerprint(trades)  # type: ignore[arg-type]

    @given(trades=_trade_lists())
    def test_entry_set_matches_the_deleted_law(self, trades: list[object]) -> None:
        assert entry_ts_us_set(trades) == _oracle_entry_ts_us_set(trades)  # type: ignore[arg-type]

    @given(
        rows=st.lists(st.tuples(_US, _US, _SIDES, _PNL), min_size=1, max_size=4),
        naive_index=st.integers(min_value=0, max_value=3),
        naive_field=st.sampled_from(["entry", "exit"]),
    )
    def test_naive_ts_escape_parity_in_field_order(
        self,
        rows: list[tuple[int, int, Side, int]],
        naive_index: int,
        naive_field: str,
    ) -> None:
        """A naive timestamp escapes with the SAME raw TypeError, per-trade
        in field order (entry µs converts before exit µs) — outcome parity
        over the whole list, whichever trade/field carries the naive stamp."""
        trades = [_trade_from_row(e, x, d, p) for e, x, d, p in rows]
        idx = naive_index % len(rows)
        entry_us, exit_us, direction, pnl = rows[idx]
        entry = _EPOCH + timedelta(microseconds=entry_us)
        exit_ = _EPOCH + timedelta(microseconds=exit_us)
        if naive_field == "entry":
            entry = entry.replace(tzinfo=None)
        else:
            exit_ = exit_.replace(tzinfo=None)
        trades[idx] = make_captured_trade(
            entry_ts=entry, exit_ts=exit_, direction=direction, pnl_paisa=pnl
        )
        oracle = _outcome(lambda: _oracle_trade_fingerprint(trades))
        live = _outcome(lambda: trade_fingerprint(trades))  # type: ignore[arg-type]
        assert oracle[0] == "exc" and oracle[1] is TypeError
        assert live == oracle
        # The entry-set law hits the SAME escape iff the naive stamp is on
        # the ENTRY field (exit stamps never enter the tier-2 signature).
        set_oracle = _outcome(lambda: _oracle_entry_ts_us_set(trades))
        set_live = _outcome(lambda: entry_ts_us_set(trades))  # type: ignore[arg-type]
        assert set_live == set_oracle


# ───────────────────────── candidate validation ──────────────────────────────

_HOSTILE_RUN_IDS = st.one_of(st.text(max_size=5), st.just(""))
_HOSTILE_MASKS = st.one_of(
    st.integers(-5, 5),
    st.booleans(),
    st.integers(min_value=-(2**130), max_value=2**130),
    st.sampled_from([None, "x", 1.5]),
)
_HOSTILE_FINGERPRINTS = st.one_of(
    st.sampled_from(
        [
            fingerprint_seed(0),
            "A" * 64,
            "g" * 64,
            "a" * 63,
            "a" * 65,
            "",
            fingerprint_seed(1) + "\n",
            None,
            7,
            1.5,
            b"ab" * 32,
        ]
    ),
    st.text(alphabet="0123456789abcdefgA \n", max_size=70),
)


class TestCandidateValidationDifferential:
    """The __post_init__ matrix (order + messages + raw escapes) vs oracle."""

    @given(run_id=_HOSTILE_RUN_IDS, mask_bits=_HOSTILE_MASKS, fingerprint=_HOSTILE_FINGERPRINTS)
    def test_construction_outcome_matches_the_deleted_law(
        self, run_id: object, mask_bits: object, fingerprint: object
    ) -> None:
        oracle = _outcome(lambda: _oracle_validate_candidate(run_id, mask_bits, fingerprint))

        def _construct() -> None:
            TwinCandidate(
                run_id=run_id,  # type: ignore[arg-type]
                mask_bits=mask_bits,  # type: ignore[arg-type]
                fingerprint=fingerprint,  # type: ignore[arg-type]
                entry_ts_us=frozenset(),
            )

        assert _outcome(_construct) == oracle


class TestMultiDefectConstructionOrder:
    """WHICH defect surfaces first — the deleted source's evaluation order."""

    @pytest.mark.parametrize(
        ("run_id", "mask_bits", "fingerprint", "expected_type", "fragment"),
        [
            ("", -1, "junk", TwinDetectError, "run_id must be non-empty"),
            ("", -1, 123, TwinDetectError, "run_id must be non-empty"),
            ("r", -1, "junk", TwinDetectError, "mask_bits must be >= 0"),
            ("r", -1, 123, TwinDetectError, "mask_bits must be >= 0"),
            ("r", True, "junk", TwinDetectError, "64-char lowercase hex"),
            ("r", False, 123, TypeError, "expected string or bytes-like object"),
            ("r", None, "junk", TypeError, "'<' not supported"),
        ],
        ids=[
            "empty_run_id_beats_mask_and_fp",
            "empty_run_id_beats_typeerror_fp",
            "negative_mask_beats_junk_fp",
            "negative_mask_beats_typeerror_fp",
            "bool_mask_passes_then_fp_refuses",
            "false_mask_passes_then_fp_raw_typeerror",
            "none_mask_raw_typeerror_beats_fp",
        ],
    )
    def test_first_defect_wins_on_both_sides(
        self,
        run_id: object,
        mask_bits: object,
        fingerprint: object,
        expected_type: type[BaseException],
        fragment: str,
    ) -> None:
        oracle = _outcome(lambda: _oracle_validate_candidate(run_id, mask_bits, fingerprint))
        live = _outcome(
            lambda: TwinCandidate(
                run_id=run_id,  # type: ignore[arg-type]
                mask_bits=mask_bits,  # type: ignore[arg-type]
                fingerprint=fingerprint,  # type: ignore[arg-type]
                entry_ts_us=frozenset(),
            )
        )
        assert oracle[0] == "exc" and oracle[1] is expected_type
        assert fragment in str(oracle[2])
        assert live == oracle

    def test_group_threshold_refusal_beats_duplicate_run_id(self) -> None:
        cand = TwinCandidate(
            run_id="dup", mask_bits=1, fingerprint=fingerprint_seed(0), entry_ts_us=frozenset()
        )
        pool = [cand, cand]
        _assert_group_parity(pool, float("nan"))
        with pytest.raises(TwinDetectError, match="jaccard_threshold"):
            group_twins(pool, jaccard_threshold=float("nan"))

    def test_first_duplicate_run_id_wins_the_message(self) -> None:
        a1 = TwinCandidate(
            run_id="a", mask_bits=1, fingerprint=fingerprint_seed(0), entry_ts_us=frozenset()
        )
        b1 = TwinCandidate(
            run_id="b", mask_bits=1, fingerprint=fingerprint_seed(1), entry_ts_us=frozenset()
        )
        pool = [a1, b1, a1, b1]
        _assert_group_parity(pool, 0.9)
        with pytest.raises(TwinDetectError, match="duplicate run_id 'a'"):
            group_twins(pool, jaccard_threshold=0.9)


# ───────────────────────── jaccard boundary family ───────────────────────────


class TestJaccardBoundaryDifferential:
    """Constructed subset shapes at the EXACT ``k/n`` threshold double."""

    @given(n=st.integers(min_value=2, max_value=30), k_raw=st.integers(min_value=1, max_value=30))
    def test_exact_ratio_threshold_is_inclusive_on_both_sides(self, n: int, k_raw: int) -> None:
        k = min(k_raw, n)
        set_a = frozenset(range(n))
        set_b = frozenset(range(k))  # subset: |∩| = k, |∪| = n
        ratio = k / n
        pool = [
            TwinCandidate(
                run_id="a", mask_bits=1, fingerprint=fingerprint_seed(0), entry_ts_us=set_a
            ),
            TwinCandidate(
                run_id="b", mask_bits=2, fingerprint=fingerprint_seed(1), entry_ts_us=set_b
            ),
        ]
        _assert_group_parity(pool, ratio)
        # The `>=` boundary is INCLUSIVE: at the exact ratio both sides merge.
        merged = group_twins(pool, jaccard_threshold=ratio)
        assert merged.family_by_run_id["b"] == "a"
        # One ULP above the ratio: parity again — and, when the bumped
        # threshold is still legal, the pair must SPLIT on both sides.
        above = math.nextafter(ratio, 2.0)
        _assert_group_parity(pool, above)
        if above <= 1.0:
            split = group_twins(pool, jaccard_threshold=above)
            assert split.family_by_run_id["b"] == "b"
        # One ULP below still merges (parity + explicit).
        below = math.nextafter(ratio, 0.0)
        _assert_group_parity(pool, below)
        assert group_twins(pool, jaccard_threshold=below).family_by_run_id["b"] == "a"

    def test_nine_of_ten_subset_is_exactly_the_pinned_default(self) -> None:
        # 9/10 == the 0.9 literal double EXACTLY — the frozen war-story shape.
        assert 9 / 10 == 0.9
        pool = [
            TwinCandidate(
                run_id="a",
                mask_bits=1,
                fingerprint=fingerprint_seed(0),
                entry_ts_us=frozenset(range(10)),
            ),
            TwinCandidate(
                run_id="b",
                mask_bits=2,
                fingerprint=fingerprint_seed(1),
                entry_ts_us=frozenset(range(9)),
            ),
        ]
        _assert_group_parity(pool, 0.9)
        assert group_twins(pool).family_by_run_id["b"] == "a"


# ───────────────────────── threshold envelope family ─────────────────────────

_HOSTILE_THRESHOLDS = st.one_of(
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(-3, 3),
    st.booleans(),
    st.sampled_from([None, "0.9", -0.0, 0.0, 1.0000001, math.nextafter(1.0, 2.0)]),
)


class TestThresholdEnvelopeDifferential:
    """The ``(0.0, 1.0]`` chained-compare law over hostile objects."""

    @given(threshold=_HOSTILE_THRESHOLDS)
    def test_envelope_outcome_matches_the_deleted_law(self, threshold: object) -> None:
        ten = frozenset(range(10))
        pool = [
            TwinCandidate(
                run_id="a", mask_bits=1, fingerprint=fingerprint_seed(0), entry_ts_us=ten
            ),
            TwinCandidate(
                run_id="b", mask_bits=2, fingerprint=fingerprint_seed(1), entry_ts_us=ten
            ),
        ]
        _assert_group_parity(pool, threshold)

    @pytest.mark.parametrize(
        "threshold",
        [0.0, -0.0, -0.1, 1.0000001, float("nan"), float("inf"), -float("inf"), 0, -1, 2, False],
        ids=[
            "zero",
            "neg_zero",
            "negative",
            "above_one",
            "nan",
            "inf",
            "neg_inf",
            "int_zero",
            "int_neg",
            "int_two",
            "false",
        ],
    )
    def test_refusal_type_and_message_parity(self, threshold: object) -> None:
        oracle = _outcome(lambda: _oracle_group_twins([], jaccard_threshold=threshold))
        live = _outcome(lambda: group_twins([], jaccard_threshold=threshold))  # type: ignore[arg-type]
        assert oracle[0] == "exc" and oracle[1] is TwinDetectError
        assert live == oracle

    def test_true_is_accepted_and_acts_as_one_point_zero(self) -> None:
        ten = frozenset(range(10))
        nine = frozenset(range(9))
        pool = [
            TwinCandidate(
                run_id="a", mask_bits=1, fingerprint=fingerprint_seed(0), entry_ts_us=ten
            ),
            TwinCandidate(
                run_id="b", mask_bits=2, fingerprint=fingerprint_seed(1), entry_ts_us=nine
            ),
        ]
        _assert_group_parity(pool, True)
        assert group_twins(pool, jaccard_threshold=True) == group_twins(pool, jaccard_threshold=1.0)


# ─────────────────────────── group partition family ──────────────────────────


class TestGroupDifferential:
    """Random pools: partition, representative, family_id + insertion ORDER."""

    @given(pool=_pools(), threshold=_VALID_THRESHOLDS)
    def test_partition_matches_the_deleted_law(
        self, pool: list[TwinCandidate], threshold: object
    ) -> None:
        _assert_group_parity(pool, threshold)

    @given(pool=_pools(), threshold=_VALID_THRESHOLDS, data=st.data())
    def test_input_order_independence_against_the_shuffled_oracle(
        self, pool: list[TwinCandidate], threshold: object, data: st.DataObject
    ) -> None:
        shuffled = data.draw(st.permutations(pool))
        # The oracle runs on the SAME shuffled order (parity) …
        _assert_group_parity(shuffled, threshold)
        # … and the live result is ORDER-INDEPENDENT (values + iteration).
        original = _outcome(lambda: group_twins(pool, jaccard_threshold=threshold))  # type: ignore[arg-type]
        reshuffled = _outcome(lambda: group_twins(shuffled, jaccard_threshold=threshold))  # type: ignore[arg-type]
        assert original[0] == "ok" == reshuffled[0]
        a = cast(TwinFamilies, original[1])
        b = cast(TwinFamilies, reshuffled[1])
        assert list(a.family_by_run_id.items()) == list(b.family_by_run_id.items())
        assert list(a.members_by_family.items()) == list(b.members_by_family.items())

    @given(pool=_pools())
    def test_str_subclass_run_ids_survive_identically(self, pool: list[TwinCandidate]) -> None:
        class RunId(str):
            __slots__ = ()

        wrapped = [
            TwinCandidate(
                run_id=RunId(c.run_id),
                mask_bits=c.mask_bits,
                fingerprint=c.fingerprint,
                entry_ts_us=c.entry_ts_us,
            )
            for c in pool
        ]
        originals = {id(c.run_id) for c in wrapped}
        _assert_group_parity(wrapped, 0.9)
        families = group_twins(wrapped)
        for key, value in families.family_by_run_id.items():
            assert id(key) in originals and id(value) in originals
        for fam, members in families.members_by_family.items():
            assert id(fam) in originals
            assert all(id(m) in originals for m in members)

    def test_mask_bit_tie_representative_is_run_id_asc(self) -> None:
        ten = frozenset(range(10))
        pool = [
            TwinCandidate(
                run_id="z-late", mask_bits=3, fingerprint=fingerprint_seed(0), entry_ts_us=ten
            ),
            TwinCandidate(
                run_id="a-early", mask_bits=5, fingerprint=fingerprint_seed(0), entry_ts_us=ten
            ),
        ]
        # Equal bit_count (both 2 bits set: 3 = 0b11, 5 = 0b101)? No — 3 and
        # 5 BOTH have bit_count 2, so the tie breaks run_id ASC: "a-early".
        assert (3).bit_count() == (5).bit_count()
        _assert_group_parity(pool, 0.9)
        assert group_twins(pool).family_by_run_id["z-late"] == "a-early"

    def test_float_mask_bit_count_attribute_error_parity(self) -> None:
        # A float mask CONSTRUCTS (1.5 < 0 is False; the fingerprint gate
        # never touches it) — then group's bit_count probe raises the SAME
        # AttributeError on both sides.
        cand = TwinCandidate(
            run_id="f",
            mask_bits=1.5,  # type: ignore[arg-type]
            fingerprint=fingerprint_seed(0),
            entry_ts_us=frozenset({1}),
        )
        oracle = _outcome(lambda: _oracle_group_twins([cand], jaccard_threshold=0.9))
        live = _outcome(lambda: group_twins([cand]))
        assert oracle[0] == "exc" and oracle[1] is AttributeError
        assert live == oracle


# ───────────────────────── disclosed envelope pins ───────────────────────────


class TestDisclosedEnvelope:
    """The scoped-out divergences pinned KNOWN — asserted BOTH ways so a
    future closure flips these pins loudly."""

    def test_beyond_i64_entry_member_refuses_where_the_oracle_grouped(self) -> None:
        cand = TwinCandidate(
            run_id="big",
            mask_bits=1,
            fingerprint=fingerprint_seed(0),
            entry_ts_us=frozenset({2**63}),
        )
        fam, _members = _oracle_group_twins([cand], jaccard_threshold=0.9)
        assert fam == {"big": "big"}
        with pytest.raises(OverflowError):
            group_twins([cand])

    def test_lone_surrogate_run_id_refuses_where_the_oracle_grouped(self) -> None:
        cand = TwinCandidate(
            run_id="r-\ud800",
            mask_bits=1,
            fingerprint=fingerprint_seed(0),
            entry_ts_us=frozenset({1}),
        )
        fam, _members = _oracle_group_twins([cand], jaccard_threshold=0.9)
        assert fam == {"r-\ud800": "r-\ud800"}
        with pytest.raises(UnicodeEncodeError):
            group_twins([cand])

    def test_fraction_threshold_compares_rounded_where_the_oracle_was_exact(self) -> None:
        # float(Fraction(1, 3)) rounds BELOW the exact rational, so a
        # jaccard of exactly that double merges crate-side (>= the rounded
        # double) while the deleted law's exact-rational compare split.
        third = Fraction(1, 3)
        assert float(third) < third
        pool = [
            TwinCandidate(
                run_id="a",
                mask_bits=1,
                fingerprint=fingerprint_seed(0),
                entry_ts_us=frozenset({1, 2, 3}),
            ),
            TwinCandidate(
                run_id="b",
                mask_bits=2,
                fingerprint=fingerprint_seed(1),
                entry_ts_us=frozenset({1}),
            ),
        ]
        fam, _members = _oracle_group_twins(pool, jaccard_threshold=third)
        assert fam["b"] == "b"  # exact-rational compare: 1/3 double < 1/3
        merged = group_twins(pool, jaccard_threshold=third)  # type: ignore[arg-type]
        assert merged.family_by_run_id["b"] == "a"  # rounded-double compare

    def test_decimal_threshold_compares_rounded_where_the_oracle_was_exact(self) -> None:
        # The Decimal twin of the Fraction pin — the SAME rounds-to-f64
        # mechanism: float(Decimal("0.333…3")) rounds BELOW the exact
        # decimal, so a jaccard of exactly that double merges crate-side
        # while the deleted law's exact compare split.
        dec_third = Decimal("0.33333333333333333333")
        assert float(dec_third) < dec_third
        pool = [
            TwinCandidate(
                run_id="a",
                mask_bits=1,
                fingerprint=fingerprint_seed(0),
                entry_ts_us=frozenset({1, 2, 3}),
            ),
            TwinCandidate(
                run_id="b",
                mask_bits=2,
                fingerprint=fingerprint_seed(1),
                entry_ts_us=frozenset({1}),
            ),
        ]
        fam, _members = _oracle_group_twins(pool, jaccard_threshold=dec_third)
        assert fam["b"] == "b"  # exact compare: 1/3 double < the Decimal
        merged = group_twins(pool, jaccard_threshold=dec_third)  # type: ignore[arg-type]
        assert merged.family_by_run_id["b"] == "a"  # rounded-double compare
