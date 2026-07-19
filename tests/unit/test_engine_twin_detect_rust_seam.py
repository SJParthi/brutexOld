"""Rust-seam parity pins for :mod:`brutex.engine.twin_detect` (slice 2d).

DEC-RUST-CASCADE-RESULTS-001 phase B: the frozen replay corpus
(``test_twin_detect_rust_parity_golden.py``) is the primary contract; this
file pins the SPLIT-EXPOSED arms the corpus does not reach — CPython ``re``'s
bytes-like ``TypeError`` escape (verified against the LIVE ``re`` oracle so
the crate's mirrored message can never drift), the per-trade field-order law
of the naive-timestamp escape (exit after entry), the int-threshold pun
beyond the corpus' bool case, the original-object preservation law of the
index-marshalled grouping output, and a hypothesis differential of the
``_jaccard`` division law against a verbatim oracle.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest
from brutex.engine.twin_detect import (
    TwinCandidate,
    _jaccard,
    group_twins,
    trade_fingerprint,
)
from brutex.strategies.types import Side
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes.wp4_twin_fixtures import fingerprint_seed, make_captured_trade

#: The deleted law's own pattern — the LIVE oracle for the escape messages.
_ORACLE_RE = re.compile(r"[0-9a-f]{64}")


def _oracle_escape(junk: object) -> str:
    """What ``re.fullmatch`` itself raises for *junk* (the frozen escape law)."""
    try:
        _ORACLE_RE.fullmatch(junk)  # type: ignore[call-overload]
    except TypeError as exc:
        return str(exc)
    raise AssertionError("oracle expected a TypeError")  # pragma: no cover


class TestFingerprintEscapeParity:
    """The raw-``TypeError`` escapes mirror CPython ``re`` verbatim."""

    @pytest.mark.parametrize(
        "junk",
        [b"ab" * 32, bytearray(b"x"), memoryview(b"y"), 1.5],
        ids=["bytes", "bytearray", "memoryview", "float"],
    )
    def test_nonstring_fingerprint_matches_the_re_oracle(self, junk: object) -> None:
        expected = _oracle_escape(junk)
        with pytest.raises(TypeError) as excinfo:
            TwinCandidate(
                run_id="r",
                mask_bits=1,
                fingerprint=junk,  # type: ignore[arg-type]
                entry_ts_us=frozenset(),
            )
        assert str(excinfo.value) == expected

    def test_naive_exit_ts_escapes_after_the_aware_entry(self) -> None:
        # The per-trade field-order law: entry µs converts first (aware —
        # fine), THEN the naive exit raises the datetime subtraction's own
        # TypeError, exactly as the deleted generator did.
        trade = make_captured_trade(
            entry_ts=datetime(2024, 1, 2, 3, 46, tzinfo=UTC),
            exit_ts=datetime(2024, 1, 2, 9, 21),  # naive ON PURPOSE
            direction=Side.BUY,
            pnl_paisa=5,
        )
        with pytest.raises(
            TypeError, match="can't subtract offset-naive and offset-aware datetimes"
        ):
            trade_fingerprint([trade])


class TestGroupSeam:
    def test_int_threshold_one_acts_as_one_point_zero(self) -> None:
        # Beyond the corpus' bool pun: an int 1 threshold passes the chained
        # compare and behaves exactly as 1.0 (only identical sets merge).
        ten = frozenset(range(10))
        cands = (
            TwinCandidate(
                run_id="t1", mask_bits=1, fingerprint=fingerprint_seed(1), entry_ts_us=ten
            ),
            TwinCandidate(
                run_id="t2", mask_bits=2, fingerprint=fingerprint_seed(2), entry_ts_us=ten
            ),
            TwinCandidate(
                run_id="t3",
                mask_bits=4,
                fingerprint=fingerprint_seed(3),
                entry_ts_us=frozenset(range(9)),
            ),
        )
        punned = group_twins(cands, jaccard_threshold=1)
        exact = group_twins(cands, jaccard_threshold=1.0)
        assert punned == exact
        assert punned.members_by_family == {"t1": ("t1", "t2"), "t3": ("t3",)}

    def test_grouping_output_carries_the_original_run_id_objects(self) -> None:
        # The index-marshalled output maps back to the caller's OWN str
        # objects — a str SUBCLASS survives with its type intact (the
        # deleted law's dict keys were the original objects too).
        class RunId(str):
            __slots__ = ()

        original = RunId("only-one")
        candidate = TwinCandidate(
            run_id=original,
            mask_bits=1,
            fingerprint=fingerprint_seed(0),
            entry_ts_us=frozenset({1}),
        )
        families = group_twins([candidate])
        (key,) = families.family_by_run_id
        assert key is original
        (fam_key,) = families.members_by_family
        assert fam_key is original
        assert families.members_by_family[fam_key][0] is original


class TestJaccardDifferential:
    """The crate division vs a VERBATIM copy of the deleted `_jaccard` law."""

    @given(
        a=st.frozensets(st.integers(min_value=-(10**18), max_value=10**18), max_size=30),
        b=st.frozensets(st.integers(min_value=-(10**18), max_value=10**18), max_size=30),
    )
    def test_jaccard_matches_the_python_oracle_bit_for_bit(
        self, a: frozenset[int], b: frozenset[int]
    ) -> None:
        expected = 1.0 if not a and not b else len(a & b) / len(a | b)
        assert repr(_jaccard(a, b)) == repr(expected)
