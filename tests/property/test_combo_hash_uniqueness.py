"""Hypothesis property tests for ``brutex.strategies.combo.combo_hash``.

Closes the gap identified in the 2026-05-25 honest-audit
(STANDARDS_100PCT_HONEST_AUDIT row 13): the existing
``test_dedup_hash_properties.py`` exercises the orchestrator's
SHA256 ``_content_hash`` (WorkItem ingest dedup) but **never touches
the actual 9-field BLAKE3 combo identity hash** mandated by
``TRACK1_BACKTEST_SPEC.md`` §"Identity (hash inputs)" / FR-BF-003 /
NFR-UNIQ-001.

Per CLAUDE.md Golden Rule #4 (reproducibility-first) every Track 1
run is identified by ``blake3(strategy_id ‖ direction ‖ instrument
‖ tf ‖ mode ‖ canonical_params ‖ data_snapshot_hash ‖
indicator_version ‖ code_commit_sha)``. Two combos collide **iff**
all nine fields collide. This module proves that contract.

Invariants covered
------------------

1. **Determinism** — same nine inputs → same digest. Reproducibility
   is the operator-locked oracle for the audit-log replay test
   (Golden Rule #7).
2. **Per-field sensitivity** — flipping any single one of the nine
   fields changes the digest. Proves no field is silently dropped on
   the wire — a regression here would silently invalidate the dedup
   contract.
3. **Params canonicalisation** — dict insertion order does NOT
   affect the digest (NFR-UNIQ-002 sort_keys=True invariant).
4. **No-duplicate-combo emission** — drawing N distinct 9-tuples
   produces N distinct digests. This is the explicit "no duplicate
   combo emitted" property the audit flagged as missing.
5. **Digest shape** — 64 lowercase hex chars (BLAKE3-256 output).
6. **Empty-string rejection** — every required string input must be
   non-empty; the validator raises ``StrategyError`` (FR-BF-003
   input-domain contract).
"""

# pyright: reportCallIssue=false
# Rationale (DEC-PYRIGHT-UNMASK-001): the Hypothesis `@st.composite` strategies
# below (e.g. `_nine_tuple`) are called WITHOUT the `draw` argument — Hypothesis
# injects it at runtime. pyright does not model the `@composite` signature
# transform and reports a false "Argument missing for parameter draw" at every
# `@given(...=_strategy())` site. The strategies are correct (the suite passes).
# Scoped to this file only; `reportCallIssue` stays a hard error everywhere else.
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from brutex.strategies.combo import canonicalise_params, combo_hash
from brutex.strategies.errors import StrategyError
from hypothesis import given, settings
from hypothesis import strategies as st

# ────────────────────────────────────────────────────────────────────────
# Hypothesis strategies for the 9 fields
# ────────────────────────────────────────────────────────────────────────

# Lowercase a-z plus digits, 1-12 chars — keeps the search space tight
# but representative of strategy IDs / instrument symbols / TFs.
_FIELD_TEXT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=1,
    max_size=12,
)

# Direction is constrained to the spec's bull/bear/neutral domain.
_DIRECTION = st.sampled_from(["bull", "bear", "neutral"])

# Mode is constrained to A/B per DEC-MODE-002 (Mode C was removed).
_MODE = st.sampled_from(["A", "B"])

# Instrument is constrained to the 4 brute-force-eligible symbols
# per CLAUDE.md §6 (NIFTY 50 / BANKNIFTY / SENSEX / INDIA VIX) — the
# spec scope-lock. INDIA VIX is REFERENCE_ONLY so it would not
# realistically appear in combo_hash inputs but is included here to
# prove the hasher is symbol-agnostic.
_INSTRUMENT = st.sampled_from(["NIFTY", "BANKNIFTY", "SENSEX", "INDIA VIX"])

# Timeframe is constrained to a representative subset of the 21 TFs
# (1m base + 19 derived intraday + 1d per CLAUDE.md §2 rule 3).
_TF = st.sampled_from(["1m", "5m", "15m", "25m", "60m", "1d"])

# Params dict: keys a-z 1-8 chars, values int / float / short str — the
# full union of types the canonicaliser accepts (excluding NaN, which
# is rejected by construction).
_PARAM_KEY = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz",
    min_size=1,
    max_size=8,
)
_PARAM_VALUE = st.one_of(
    st.integers(min_value=-(10**6), max_value=10**6),
    st.floats(allow_nan=False, allow_infinity=False, width=32),
    st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_",
        min_size=0,
        max_size=8,
    ),
)
_PARAMS = st.dictionaries(keys=_PARAM_KEY, values=_PARAM_VALUE, min_size=0, max_size=6)


@st.composite
def _nine_tuple(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a complete 9-field combo identity tuple."""
    return {
        "strategy_id": draw(_FIELD_TEXT),
        "direction": draw(_DIRECTION),
        "instrument": draw(_INSTRUMENT),
        "tf": draw(_TF),
        "mode": draw(_MODE),
        "params": draw(_PARAMS),
        "data_snapshot_hash": draw(_FIELD_TEXT),
        "indicator_version": draw(_FIELD_TEXT),
        "code_commit_sha": draw(_FIELD_TEXT),
    }


def _hash(tup: Mapping[str, Any]) -> str:
    return combo_hash(**tup)


def _canonical_identity(tup: Mapping[str, Any]) -> str:
    """Project a 9-tuple onto the exact quotient ``combo_hash`` is injective over.

    ``canonicalise_params`` DELIBERATELY collapses param values the hasher
    treats as ONE run identity — signed zero (``-0.0`` → ``+0.0``,
    DEC-SWEEP-DEDUP-LMDB-001 / Golden Rule #4) and dict insertion order
    (``sort_keys=True``) — so a raw ``str(dict)`` projection would present
    e.g. ``{"a": 0.0}`` vs ``{"a": -0.0}`` as "distinct" tuples that
    correctly hash equal, falsifying the injectivity property below
    (hostile-review de-flake, 2026-07-07: distinctness must be judged in
    the canonical-params quotient, never the raw string form).
    """
    return str({**tup, "params": canonicalise_params(tup["params"])})


# ────────────────────────────────────────────────────────────────────────
# Invariant 1 — determinism
# ────────────────────────────────────────────────────────────────────────


class TestDeterminism:
    @given(tup=_nine_tuple())
    @settings(max_examples=200, deadline=None)
    def test_same_inputs_same_hash(self, tup: dict[str, Any]) -> None:
        assert _hash(tup) == _hash(tup)

    @given(tup=_nine_tuple())
    @settings(max_examples=200, deadline=None)
    def test_three_invocations_same_hash(self, tup: dict[str, Any]) -> None:
        h1 = _hash(tup)
        h2 = _hash(dict(tup))
        h3 = _hash({**tup})
        assert h1 == h2 == h3


# ────────────────────────────────────────────────────────────────────────
# Invariant 2 — per-field sensitivity (no field silently dropped)
# ────────────────────────────────────────────────────────────────────────


_STRING_FIELDS = (
    "strategy_id",
    "direction",
    "instrument",
    "tf",
    "mode",
    "data_snapshot_hash",
    "indicator_version",
    "code_commit_sha",
)


class TestPerFieldSensitivity:
    @pytest.mark.parametrize("field", _STRING_FIELDS)
    @given(tup=_nine_tuple())
    @settings(max_examples=50, deadline=None)
    def test_mutating_any_string_field_changes_hash(self, field: str, tup: dict[str, Any]) -> None:
        original = _hash(tup)
        # Mutate by appending a sentinel character that the alphabet
        # does not include (so the mutated value is guaranteed different).
        mutated = dict(tup)
        mutated[field] = str(tup[field]) + "!"
        assert _hash(mutated) != original

    @given(tup=_nine_tuple(), extra_key=_PARAM_KEY, extra_val=_PARAM_VALUE)
    @settings(max_examples=100, deadline=None)
    def test_adding_param_changes_hash(
        self, tup: dict[str, Any], extra_key: str, extra_val: Any
    ) -> None:
        # Only meaningful if the new key isn't already present.
        if extra_key in tup["params"]:
            return
        original = _hash(tup)
        mutated = dict(tup)
        mutated["params"] = {**tup["params"], extra_key: extra_val}
        assert _hash(mutated) != original


# ────────────────────────────────────────────────────────────────────────
# Invariant 3 — params canonicalisation (order-invariance)
# ────────────────────────────────────────────────────────────────────────


class TestParamsCanonicalisation:
    @given(tup=_nine_tuple())
    @settings(max_examples=200, deadline=None)
    def test_params_insertion_order_does_not_matter(self, tup: dict[str, Any]) -> None:
        original = _hash(tup)
        reversed_params = dict(reversed(list(tup["params"].items())))
        mutated = {**tup, "params": reversed_params}
        assert _hash(mutated) == original

    @given(tup=_nine_tuple())
    @settings(max_examples=100, deadline=None)
    def test_params_via_sorted_dict_matches(self, tup: dict[str, Any]) -> None:
        original = _hash(tup)
        sorted_params = dict(sorted(tup["params"].items()))
        mutated = {**tup, "params": sorted_params}
        assert _hash(mutated) == original


# ────────────────────────────────────────────────────────────────────────
# Invariant 4 — no-duplicate-combo emission (the audit-flagged gap)
# ────────────────────────────────────────────────────────────────────────


class TestNoDuplicateComboEmitted:
    @given(tuples=st.lists(_nine_tuple(), min_size=2, max_size=20, unique_by=_canonical_identity))
    @settings(max_examples=50, deadline=None)
    def test_distinct_tuples_produce_distinct_hashes(self, tuples: list[dict[str, Any]]) -> None:
        """Drawing N pairwise-distinct 9-tuples must produce N distinct digests.

        This is the explicit "no duplicate combo emitted" property the
        2026-05-25 honest-audit (STANDARDS_100PCT_HONEST_AUDIT row 13)
        flagged as missing. The orchestrator's dedup contract assumes
        ``combo_hash`` is injective on the 9-tuple domain — if two
        distinct combos hashed to the same digest the L1 Bloom + L2 LMDB
        layers would silently coalesce them and the dedup invariant
        would break.

        Pairwise distinctness is judged through ``_canonical_identity``
        (the canonical-params quotient), NEVER raw ``str``: the
        canonicaliser deliberately collapses signed zero and param
        insertion order, so raw-string-distinct dicts like
        ``{"a": 0.0}`` / ``{"a": -0.0}`` are ONE combo by design and
        must not be presented to the injectivity assertion as a
        "collision". Hypothesis shrinks toward minimal collisions.
        """
        hashes = [_hash(t) for t in tuples]
        assert len(hashes) == len(set(hashes)), (
            f"Collision: {len(hashes) - len(set(hashes))} duplicate hashes "
            f"emitted from {len(tuples)} distinct 9-tuples"
        )

    def test_signed_zero_params_are_one_combo(self) -> None:
        """Regression (hostile-review de-flake, 2026-07-07): the exact
        hypothesis falsifying example that flaked under the old
        ``unique_by=str`` projection. Params ``{"a": 0.0}`` and
        ``{"a": -0.0}`` are ONE run identity — ``canonicalise_params``
        normalises signed zero BEFORE repr (Golden Rule #4
        reproducibility) — so the digests MUST be equal AND the
        ``_canonical_identity`` projection MUST collapse the pair, so the
        injectivity property above can never draw them as two "distinct"
        tuples again.
        """
        base: dict[str, Any] = {
            "strategy_id": "s1",
            "direction": "bull",
            "instrument": "NIFTY",
            "tf": "5m",
            "mode": "A",
            "data_snapshot_hash": "snap",
            "indicator_version": "v1",
            "code_commit_sha": "c0ffee",
        }
        pos = {**base, "params": {"a": 0.0}}
        neg = {**base, "params": {"a": -0.0}}
        assert _hash(pos) == _hash(neg)
        assert _canonical_identity(pos) == _canonical_identity(neg)


# ────────────────────────────────────────────────────────────────────────
# Invariant 5 — digest shape
# ────────────────────────────────────────────────────────────────────────


class TestDigestShape:
    @given(tup=_nine_tuple())
    @settings(max_examples=100, deadline=None)
    def test_hash_is_64_lower_hex(self, tup: dict[str, Any]) -> None:
        h = _hash(tup)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)


# ────────────────────────────────────────────────────────────────────────
# Invariant 6 — empty-string rejection (FR-BF-003 input-domain contract)
# ────────────────────────────────────────────────────────────────────────


class TestEmptyStringRejection:
    @pytest.mark.parametrize("field", _STRING_FIELDS)
    @given(tup=_nine_tuple())
    @settings(max_examples=20, deadline=None)
    def test_empty_string_field_raises(self, field: str, tup: dict[str, Any]) -> None:
        mutated = dict(tup)
        mutated[field] = ""
        with pytest.raises(StrategyError, match=field):
            _hash(mutated)


# ────────────────────────────────────────────────────────────────────────
# Invariant 7 — signed-zero split (the SAFE dedup direction)
# ────────────────────────────────────────────────────────────────────────


class TestSignedZero:
    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    @settings(max_examples=200, deadline=None)
    def test_signed_zero_collapses_when_only_sign_differs(self, value: float) -> None:
        """DEC-SWEEP-DEDUP-LMDB-001: signed zero is normalised (``-0.0`` → ``+0.0``)
        BEFORE repr, so two floats whose ``repr`` differs ONLY by the sign of a
        zero collapse to one token; any other repr difference stays distinct.
        The canonical token is exactly ``repr(value + 0.0)``."""
        pos = canonicalise_params({"x": value})
        neg = canonicalise_params({"x": -value})
        if repr(value + 0.0) == repr(-value + 0.0):
            assert pos == neg
        else:
            assert pos != neg

    def test_signed_zero_explicit(self) -> None:
        # DEC-SWEEP-DEDUP-LMDB-001: -0.0 collapses to +0.0 (was: distinct).
        assert canonicalise_params({"x": 0.0}) == canonicalise_params({"x": -0.0})


# ────────────────────────────────────────────────────────────────────────
# Invariant 8 — float / str / Decimal alias holds; distinct text stays distinct
# ────────────────────────────────────────────────────────────────────────


class TestFloatStrDecimalAlias:
    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    @settings(max_examples=200, deadline=None)
    def test_float_str_alias(self, value: float) -> None:
        """A float and the ``str`` of its canonical ``repr`` ALWAYS collapse to one
        token — the float canonicalises to ``repr(value + 0.0)`` (signed zero is
        normalised first, DEC-SWEEP-DEDUP-LMDB-001), so the aliasing str is the
        repr of the SIGN-NORMALISED value (wire-locked alias; mitigated by
        one-type-per-axis at ParamGrid)."""
        text = repr(value + 0.0)
        as_float = canonicalise_params({"k": value})
        as_str = canonicalise_params({"k": text})
        assert as_float == as_str

    @given(value=st.floats(allow_nan=False, allow_infinity=False))
    @settings(max_examples=200, deadline=None)
    def test_decimal_aliases_float_iff_str_matches_repr(self, value: float) -> None:
        """A ``Decimal`` aliases the float IFF ``str(Decimal)`` equals the float's
        CANONICAL ``repr`` (``repr(value + 0.0)`` — signed zero normalised first,
        DEC-SWEEP-DEDUP-LMDB-001) — Decimal can normalise exponent CASE
        (``1e+16`` -> ``1E+16``), an honest non-universal alias (Golden Rule #10).
        The token is exactly the ``Decimal``'s ``str`` form either way."""
        canonical_text = repr(value + 0.0)
        as_float = canonicalise_params({"k": value})
        dec = Decimal(canonical_text)
        as_decimal = canonicalise_params({"k": dec})
        if str(dec) == canonical_text:
            assert as_decimal == as_float
        else:
            # Decimal's str diverged from the float repr (case/scale normalise).
            assert as_decimal != as_float
        # The Decimal token is its own str(), always.
        assert as_decimal == f'{{"k":"{dec!s}"}}'

    @given(a=_FIELD_TEXT, b=_FIELD_TEXT)
    @settings(max_examples=200, deadline=None)
    def test_distinct_text_stays_distinct(self, a: str, b: str) -> None:
        """Two distinct string param values never collide on canonicalisation."""
        if a == b:
            return
        assert canonicalise_params({"k": a}) != canonicalise_params({"k": b})
