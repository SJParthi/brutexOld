"""Deterministic input corpus for the twin_detect parity goldens.

DEC-RUST-CASCADE-RESULTS-001 phase A (slice 2d): the T14 lane will convert the
``brutex.engine.twin_detect`` law families to the ``rust/brutex-cascade`` crate
under the compressed greenfield cycle (capture-first — this corpus is frozen
against the UNTOUCHED Python module BEFORE any conversion). This module is the
ONE builder of the parity corpus INPUTS *and* the shared scenario RUNNERS —
shared verbatim by

* the capture generator (``gen_cascade_rust_parity.py`` — run ONCE against the
  pre-conversion pure-Python implementations; fixture frozen), and
* the replay golden (``test_twin_detect_rust_parity_golden.py`` — drives the
  post-conversion Rust-backed API against the frozen fixture),

so the two sides can never drift on what was fed in.

SCOPE: the WHOLE module is pure (no I/O, no wall clock, no globals) and every
family is a conversion target — the ``bxtwin1`` fingerprint serialization law
(frozen transitively through its BLAKE3 digests), the entry-timestamp
signature, the :class:`TwinCandidate` validation matrix (bool==int puns and the
raw ``TypeError`` escapes included), the ``_jaccard`` division law (exact f64
reprs), and the two-tier ``group_twins`` union-find partition (the INCLUSIVE
0.90 boundary, transitivity, the fewest-bits-then-run_id representative law,
the ``family_id``-ASC members iteration order, input-order independence, and
the full refusal matrix).

Every case is fully deterministic: fixed timestamps (built from explicit
UTC/IST instants), fixed run_ids/fingerprints, no RNG, no temp paths — nothing
in a captured value is machine-dependent. Inputs are built in CODE (never
parsed from the fixture) because several hostile cases carry values JSON
cannot round-trip faithfully as INPUTS (naive datetimes, bool-vs-int mask
puns, non-string fingerprints).

The consumed laws stay Python and are treated as INPUT builders, not targets:
:class:`~brutex.engine.trade_capture.CapturedTrade` construction (its own
validation is trade_capture's law — the corpus feeds only constructible
instances, plus the one naive-timestamp shape it deliberately does NOT refuse,
which escapes ``trade_fingerprint`` as a raw ``TypeError`` — frozen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from brutex.engine.twin_detect import (
    TwinCandidate,
    TwinDetectError,
    _jaccard,
    entry_ts_us_set,
    group_twins,
    trade_fingerprint,
)
from brutex.strategies.types import Side

from tests.fakes.wp4_twin_fixtures import fingerprint_seed, make_captured_trade

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brutex.engine.trade_capture import CapturedTrade

__all__ = [
    "GroupCase",
    "candidate_cases",
    "entry_set_cases",
    "fingerprint_cases",
    "group_cases",
    "jaccard_cases",
    "run_candidate_case",
    "run_entry_set_case",
    "run_fingerprint_case",
    "run_group_case",
    "run_jaccard_case",
]

#: 2024-01-02 09:16 IST as UTC (mirrors the WP4 fixture anchor) — 03:46 UTC.
_T0 = datetime(2024, 1, 2, 3, 46, tzinfo=UTC)


def _ts(minutes: int, *, microseconds: int = 0) -> datetime:
    """A deterministic tz-aware UTC instant offset from the fixture anchor."""
    return _T0 + timedelta(minutes=minutes, microseconds=microseconds)


def _trade(
    entry_minute: int,
    hold_minutes: int,
    direction: Side,
    pnl_paisa: int,
    *,
    entry_us: int = 0,
    exit_us: int = 0,
) -> CapturedTrade:
    """A valid CapturedTrade with only the four identity fields varying."""
    return make_captured_trade(
        entry_ts=_ts(entry_minute, microseconds=entry_us),
        exit_ts=_ts(entry_minute + hold_minutes, microseconds=exit_us),
        direction=direction,
        pnl_paisa=pnl_paisa,
    )


# ──────────────────────────────────────────────────────────────────────────
# trade_fingerprint — the bxtwin1 serialization law, frozen via its digests
# ──────────────────────────────────────────────────────────────────────────


def _multi_trades() -> list[CapturedTrade]:
    """The canonical 3-trade mixed-direction chronological fixture."""
    return [
        _trade(0, 5, Side.BUY, 1_250),
        _trade(30, 25, Side.SELL, -475),
        _trade(120, 1, Side.BUY, 0),
    ]


def fingerprint_cases() -> list[tuple[str, Sequence[CapturedTrade]]]:
    """Trade lists whose exact BLAKE3 digests pin the serialization bytes."""
    pre_epoch = make_captured_trade(
        entry_ts=datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        exit_ts=datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC),
        direction=Side.BUY,
        pnl_paisa=7,
    )
    far_future = make_captured_trade(
        entry_ts=datetime(9999, 12, 31, 22, 0, tzinfo=UTC),
        exit_ts=datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        direction=Side.SELL,
        pnl_paisa=-1,
    )
    naive_entry = make_captured_trade(
        entry_ts=datetime(2024, 1, 2, 9, 16),  # naive ON PURPOSE — the escape law
        exit_ts=_ts(5),
        direction=Side.BUY,
        pnl_paisa=100,
    )
    # Two trades whose IDENTITY projections are equal while every non-identity
    # field differs — the fixture freezes the SAME digest for both cases.
    identity_a = make_captured_trade(
        entry_ts=_ts(10), exit_ts=_ts(20), direction=Side.BUY, pnl_paisa=999
    )
    identity_b = make_captured_trade(
        entry_ts=_ts(10),
        exit_ts=_ts(20),
        direction=Side.BUY,
        pnl_paisa=999,
        exit_reason="stop_loss",
        mae_paisa=123,
        mfe_paisa=456,
    )
    return [
        ("empty_list_bare_prefix", []),
        ("single_buy", [_trade(0, 5, Side.BUY, 100)]),
        ("single_sell_same_shape", [_trade(0, 5, Side.SELL, 100)]),
        ("multi_chronological", _multi_trades()),
        ("multi_reversed_order_differs", list(reversed(_multi_trades()))),
        ("negative_pnl", [_trade(0, 5, Side.BUY, -(2**62))]),
        ("zero_pnl", [_trade(0, 5, Side.BUY, 0)]),
        (
            "duplicate_timestamps_pair",
            [_trade(3, 4, Side.BUY, 10), _trade(3, 4, Side.BUY, 10)],
        ),
        (
            "microsecond_precision",
            [_trade(0, 5, Side.BUY, 55, entry_us=123456, exit_us=123457)],
        ),
        ("pre_epoch_negative_us", [pre_epoch]),
        ("far_future_year_9999", [far_future]),
        ("exit_before_entry_serializes", [_trade(10, -5, Side.SELL, 42)]),
        ("identity_only_a", [identity_a]),
        ("identity_only_b_extras_differ", [identity_b]),
        ("naive_entry_ts_escape", [naive_entry]),
    ]


def run_fingerprint_case(trades: Sequence[CapturedTrade]) -> dict[str, Any]:
    """The digest, or the raw exception (a naive ts escapes as TypeError)."""
    try:
        return {"fingerprint": trade_fingerprint(trades)}
    except BaseException as exc:  # freeze the VERBATIM escape law
        return {"error_type": type(exc).__name__, "error_str": str(exc)}


# ──────────────────────────────────────────────────────────────────────────
# entry_ts_us_set — the tier-2 signature (exact epoch-µs frozensets)
# ──────────────────────────────────────────────────────────────────────────


def entry_set_cases() -> list[tuple[str, Sequence[CapturedTrade]]]:
    pre_epoch = make_captured_trade(
        entry_ts=datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        exit_ts=_ts(0),
        direction=Side.BUY,
        pnl_paisa=1,
    )
    return [
        ("empty", []),
        ("single_exact_us", [_trade(0, 5, Side.BUY, 1)]),
        (
            "duplicate_entries_collapse",
            [_trade(7, 5, Side.BUY, 1), _trade(7, 9, Side.SELL, -2)],
        ),
        (
            "mixed_with_microseconds",
            [
                _trade(0, 5, Side.BUY, 1, entry_us=1),
                _trade(1, 5, Side.SELL, 2, entry_us=999999),
            ],
        ),
        ("pre_epoch_negative", [pre_epoch]),
    ]


def run_entry_set_case(trades: Sequence[CapturedTrade]) -> list[int]:
    """The signature as a SORTED list (frozensets are not JSON-stable)."""
    return sorted(entry_ts_us_set(trades))


# ──────────────────────────────────────────────────────────────────────────
# TwinCandidate — the validation matrix (puns + raw escapes included)
# ──────────────────────────────────────────────────────────────────────────


def candidate_cases() -> list[tuple[str, Callable[[], TwinCandidate]]]:
    """Thunks — several inputs (bools, non-strings) are not JSON-portable."""
    hex64 = fingerprint_seed(0)

    def make(**kwargs: Any) -> Callable[[], TwinCandidate]:
        base: dict[str, Any] = {
            "run_id": "run-a",
            "mask_bits": 0b101,
            "fingerprint": hex64,
            "entry_ts_us": frozenset({1, 2}),
        }
        base.update(kwargs)
        return lambda: TwinCandidate(**base)

    def from_trades() -> TwinCandidate:
        return TwinCandidate.from_trades(
            run_id="run-from-trades", mask_bits=0b11, trades=_multi_trades()
        )

    return [
        ("ok_minimal", make()),
        ("ok_mask_zero", make(mask_bits=0)),
        ("ok_mask_bool_true_pun", make(mask_bits=True)),
        ("ok_mask_bool_false_pun", make(mask_bits=False)),
        ("ok_unicode_run_id", make(run_id="π-run-☂")),
        ("ok_whitespace_run_id_is_truthy", make(run_id=" ")),
        ("ok_from_trades_binds_one_list", from_trades),
        ("empty_run_id_refused", make(run_id="")),
        ("negative_mask_bits_refused", make(mask_bits=-1)),
        ("fp_uppercase_refused", make(fingerprint=hex64.upper())),
        ("fp_63_chars_refused", make(fingerprint=hex64[:63])),
        ("fp_65_chars_refused", make(fingerprint=hex64 + "a")),
        ("fp_nonhex_refused", make(fingerprint="g" * 64)),
        ("fp_empty_refused", make(fingerprint="")),
        ("fp_trailing_newline_refused", make(fingerprint=hex64[:63] + "\n")),
        ("fp_int_type_escape", make(fingerprint=5)),
        ("fp_none_type_escape", make(fingerprint=None)),
    ]


def run_candidate_case(thunk: Callable[[], TwinCandidate]) -> dict[str, Any]:
    """The constructed echo (mask as repr — bool puns survive) or the error."""
    try:
        candidate = thunk()
    except BaseException as exc:  # freeze the VERBATIM escape law
        return {"error_type": type(exc).__name__, "error_str": str(exc)}
    return {
        "run_id": candidate.run_id,
        "mask_bits_repr": repr(candidate.mask_bits),
        "bit_count": candidate.mask_bits.bit_count(),
        "fingerprint": candidate.fingerprint,
        "entry_ts_us": sorted(candidate.entry_ts_us),
    }


# ──────────────────────────────────────────────────────────────────────────
# _jaccard — the division law (exact f64 reprs)
# ──────────────────────────────────────────────────────────────────────────


def jaccard_cases() -> list[tuple[str, tuple[frozenset[int], frozenset[int]]]]:
    return [
        ("empty_empty_defined_identical", (frozenset(), frozenset())),
        ("empty_vs_nonempty", (frozenset(), frozenset({1}))),
        ("nonempty_vs_empty", (frozenset({1, 2}), frozenset())),
        ("disjoint", (frozenset({1, 2}), frozenset({3, 4}))),
        ("identical", (frozenset({1, 2, 3}), frozenset({1, 2, 3}))),
        ("one_of_three", (frozenset({1, 2}), frozenset({2, 3}))),
        ("half", (frozenset({1, 2}), frozenset({1, 3}))),
        (
            # |∩|=9, |∪|=10 → 9/10 == the 0.9 double EXACTLY (subset shape).
            "nine_of_ten_exact_boundary_value",
            (frozenset(range(10)), frozenset(range(9))),
        ),
        (
            "nine_of_eleven_below_boundary",
            (frozenset(range(10)), frozenset(range(9)) | {99}),
        ),
        (
            "seventeen_of_nineteen",
            (frozenset(range(18)), frozenset(range(17)) | {99}),
        ),
        ("subset_five_of_ten", (frozenset(range(5)), frozenset(range(10)))),
    ]


def run_jaccard_case(pair: tuple[frozenset[int], frozenset[int]]) -> str:
    """The exact f64 repr — the |∩|/|∪| division parity is the frozen law."""
    return repr(_jaccard(pair[0], pair[1]))


# ──────────────────────────────────────────────────────────────────────────
# group_twins — the two-tier union-find partition
# ──────────────────────────────────────────────────────────────────────────

_DEFAULT = object()


@dataclass(frozen=True)
class GroupCase:
    """One group_twins invocation.

    ``threshold`` is the raw kwarg (``None`` = omit → the module default;
    bools/junk pass through verbatim to freeze the puns/refusals).
    ``orderings`` are index permutations of ``candidates`` — the runner
    replays EVERY ordering and asserts the results identical (the
    input-order-independence law), then captures the canonical one.
    """

    candidates: tuple[TwinCandidate, ...]
    threshold: Any = None
    orderings: tuple[tuple[int, ...], ...] = field(default_factory=tuple)


def _cand(
    run_id: str,
    *,
    mask_bits: int = 0b1,
    seed: int = 0,
    entries: frozenset[int] = frozenset(),
) -> TwinCandidate:
    return TwinCandidate(
        run_id=run_id,
        mask_bits=mask_bits,
        fingerprint=fingerprint_seed(seed),
        entry_ts_us=entries,
    )


def group_cases() -> list[tuple[str, GroupCase]]:
    ten = frozenset(range(10))
    nine_of_ten = frozenset(range(9))  # SUBSET of `ten`: jaccard == 0.9 exactly
    seventeen_of_nineteen = frozenset(range(17)) | {99}  # vs range(18): ~0.8947

    # The transitive drift chain: A~B and B~C are 29/32 ≈ 0.906 ≥ 0.90 while
    # A~C alone is 28/32 = 0.875 < 0.90 — one family only via union-find.
    chain_a = frozenset(range(1, 31))
    chain_b = frozenset(range(2, 32))
    chain_c = frozenset(range(3, 33))

    mixed_pool = (
        _cand("r-01", mask_bits=0b111, seed=1, entries=ten),
        _cand("r-02", mask_bits=0b1000, seed=2, entries=nine_of_ten),
        _cand("r-03", mask_bits=0b1, seed=3, entries=frozenset({500, 501})),
        _cand("r-04", mask_bits=0b11, seed=3, entries=frozenset({900})),
        _cand("r-05", mask_bits=0b1, seed=5, entries=frozenset({700})),
    )
    return [
        ("empty_pool", GroupCase(candidates=())),
        ("singleton", GroupCase(candidates=(_cand("only", seed=1, entries=ten),))),
        (
            "tier1_same_fp_disjoint_entries_merge",
            GroupCase(
                candidates=(
                    _cand("a-three-bits", mask_bits=0b111, seed=7, entries=frozenset({1})),
                    _cand("b-one-bit", mask_bits=0b1000_0000, seed=7, entries=frozenset({2})),
                )
            ),
        ),
        (
            "tier1_rep_tie_breaks_run_id_asc",
            GroupCase(
                candidates=(
                    _cand("zzz", mask_bits=0b11, seed=9),
                    _cand("aaa", mask_bits=0b101, seed=9),
                )
            ),
        ),
        (
            "tier2_boundary_0_90_inclusive",
            GroupCase(
                candidates=(
                    _cand("x1", mask_bits=0b1, seed=11, entries=ten),
                    _cand("x2", mask_bits=0b11, seed=12, entries=nine_of_ten),
                )
            ),
        ),
        (
            "tier2_just_below_stays_separate",
            GroupCase(
                candidates=(
                    _cand("y1", seed=13, entries=frozenset(range(18))),
                    _cand("y2", seed=14, entries=seventeen_of_nineteen),
                )
            ),
        ),
        (
            "tier2_transitive_chain_one_family",
            GroupCase(
                candidates=(
                    _cand("c-a", mask_bits=0b111, seed=15, entries=chain_a),
                    _cand("c-b", mask_bits=0b11, seed=16, entries=chain_b),
                    _cand("c-c", mask_bits=0b1, seed=17, entries=chain_c),
                ),
                orderings=((0, 1, 2), (2, 1, 0), (1, 2, 0)),
            ),
        ),
        (
            "threshold_one_only_identical_merge",
            GroupCase(
                candidates=(
                    _cand("t1", seed=18, entries=ten),
                    _cand("t2", seed=19, entries=ten),
                    _cand("t3", seed=20, entries=nine_of_ten),
                ),
                threshold=1.0,
            ),
        ),
        (
            "threshold_bool_true_pun_acts_as_one",
            GroupCase(
                candidates=(
                    _cand("t1", seed=18, entries=ten),
                    _cand("t2", seed=19, entries=ten),
                    _cand("t3", seed=20, entries=nine_of_ten),
                ),
                threshold=True,
            ),
        ),
        (
            "empty_entry_sets_are_identical_behavior",
            GroupCase(
                candidates=(
                    _cand("e1", mask_bits=0b1, seed=21),
                    _cand("e2", mask_bits=0b11, seed=22),
                )
            ),
        ),
        (
            "custom_threshold_half_inclusive",
            GroupCase(
                candidates=(
                    _cand("h1", seed=23, entries=frozenset(range(5))),
                    _cand("h2", seed=24, entries=frozenset(range(10))),
                ),
                threshold=0.5,
            ),
        ),
        (
            "mixed_pool_two_families_two_singletons",
            GroupCase(
                candidates=mixed_pool,
                orderings=((0, 1, 2, 3, 4), (4, 3, 2, 1, 0), (2, 0, 4, 1, 3)),
            ),
        ),
        (
            "all_identical_fp_one_family",
            GroupCase(
                candidates=tuple(
                    _cand(f"m-{i:02d}", mask_bits=(1 << i) | 0b1, seed=42) for i in range(6)
                ),
                orderings=((0, 1, 2, 3, 4, 5), (5, 4, 3, 2, 1, 0)),
            ),
        ),
        (
            "duplicate_run_id_refused",
            GroupCase(candidates=(_cand("dup", seed=1), _cand("dup", seed=2))),
        ),
        ("threshold_zero_refused", GroupCase(candidates=(), threshold=0.0)),
        ("threshold_negative_zero_refused", GroupCase(candidates=(), threshold=-0.0)),
        ("threshold_negative_refused", GroupCase(candidates=(), threshold=-0.1)),
        ("threshold_above_one_refused", GroupCase(candidates=(), threshold=1.0000001)),
        ("threshold_nan_refused", GroupCase(candidates=(), threshold=float("nan"))),
        ("threshold_inf_refused", GroupCase(candidates=(), threshold=float("inf"))),
    ]


def _families_to_jsonable(candidates: Sequence[TwinCandidate], threshold: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {} if threshold is None else {"jaccard_threshold": threshold}
    families = group_twins(candidates, **kwargs)
    return {
        "family_by_run_id": dict(sorted(families.family_by_run_id.items())),
        # ITERATION order captured deliberately — the family_id-ASC key
        # insertion order is part of the frozen law.
        "members_by_family": [
            [family_id, list(members)] for family_id, members in families.members_by_family.items()
        ],
    }


def run_group_case(case: GroupCase) -> dict[str, Any]:
    """The serialized partition (order-independence verified in-runner) or error."""
    try:
        canonical = _families_to_jsonable(case.candidates, case.threshold)
    except TwinDetectError as exc:
        return {"error_type": type(exc).__name__, "error_str": str(exc)}
    for ordering in case.orderings:
        permuted = tuple(case.candidates[i] for i in ordering)
        assert _families_to_jsonable(permuted, case.threshold) == canonical, (
            f"input-order independence violated for ordering {ordering}"
        )
    return canonical
