"""The 60→66 vocab-reshape migration invariant — the permanent back-compat harness.

The reshape (VWAP bits 52/53 tombstoned + their math deleted; CPR bits 60-63
implemented; SuperTrend bits 64/65 declared layout-only) regenerated the frozen
400-case oracle ``fixtures/vocab_golden_vectors.json`` under the
FROZEN-INPUT migration discipline (GOLD-01 — never a free
regenerate-and-trust):

* the generator BURNS the retired VWAP RNG draw in its historical position, so
  every case's INPUT fields are byte-identical to the pre-reshape (version-2)
  fixture — pinned here per case via the committed
  ``fixtures/vocab_golden_migration_v2_masks.json`` companion's per-case input
  SHA-256s;
* per case, the NEW mask relates to the OLD mask by EXACTLY the reshape:
  bits 0-59 unchanged except the tombstones cleared
  (``new & ((1<<60)-1) == old & ~((1<<52)|(1<<53))``) — **this is the reshape
  back-compat harness: the 58 pre-existing active conditions (bits 0-51 +
  54-59) evaluate bit-identically pre/post reshape over the frozen inputs**;
* no bit at or above 64 is ever set (``new >> 64 == 0``);
* the only NEW bits (60-63) are exactly justified by the CPR arithmetic
  recomputed INDEPENDENTLY here from the case's own prev-day anchors (exact
  integer/Decimal math — never the production helper).

DEC-GAP-MIDPOINT-CONDITIONS-001 (the 66→69 growth) later APPENDED the
gap-midpoint bits 66-68 with exactly two per-case input keys drawn from a
SEPARATE second RNG stream — the pre-reshape invariants above still hold
verbatim over the pre-existing fields/bits (the appended keys are enumerated
and excluded from the input hash; the gap bits are the one sanctioned >= 64
firing exception).

The companion fixture is a committed copy of the OLD masks: its ``note`` field
records the provenance prose, and its sibling ``source_fixture_sha256`` key
records the pre-reshape fixture's WHOLE-FILE SHA-256
(``19640054cb39…`` — verified against
``git show d6ac9f850:tests/golden/fixtures/vocab_golden_vectors.json`` and
pinned literally below), so this invariant stays verifiable forever — a later
regeneration that silently moved an input or an old-vocabulary bit fails
loudly here.
"""

from __future__ import annotations

import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Any

_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_NEW = json.loads((_FIXTURE_DIR / "vocab_golden_vectors.json").read_text(encoding="utf-8"))
_OLD = json.loads(
    (_FIXTURE_DIR / "vocab_golden_migration_v2_masks.json").read_text(encoding="utf-8")
)

#: The two RETIRED tombstone bits the reshape cleared.
_TOMBSTONE_MASK = (1 << 52) | (1 << 53)
#: The per-case input keys APPENDED by DEC-GAP-MIDPOINT-CONDITIONS-001 (bits
#: 66-68, drawn from a SEPARATE second RNG stream so every pre-existing draw is
#: unshifted) — excluded from the pre-reshape input hash, enumerated so any
#: OTHER new key still breaks the invariant.
_GAP_APPENDED_KEYS = frozenset({"gap_mid_paisa"})
_GAP_APPENDED_PROXIMITY_KEYS = frozenset({"gap_mid_bps"})
#: The implemented gap-midpoint trio (bits 66-68).
_GAP_MASK = 0b111 << 66
#: The pre-reshape 60-bit vocabulary span.
_LOW60 = (1 << 60) - 1
#: The narrow-CPR width threshold (basis points of P) — restated literally
#: (the cross-pin against the wheel export lives in the vocabulary unit suite).
_NARROW_BPS = 30


def _input_sha(case: dict[str, Any]) -> str:
    """SHA-256 of a case's PRE-RESHAPE input fields (everything except
    ``expected_mask`` and the DEC-GAP-MIDPOINT-CONDITIONS-001 appends).

    Canonical JSON — ``sort_keys`` + compact separators — exactly how the
    companion fixture's ``inputs_sha256`` values were minted from the OLD
    (version-2) fixture. The gap growth (bits 66-68) APPENDED exactly two
    per-case input keys drawn from a SEPARATE second RNG stream
    (``gap_mid_paisa`` + ``proximity.gap_mid_bps`` — the enumerated
    ``_GAP_APPENDED_KEYS``); those are EXCLUDED here so the invariant stays
    exactly the one the companion minted: every pre-existing input field is
    byte-identical to the frozen pre-reshape corpus. A NEW key outside the
    enumerated append-set still fails loudly (it lands in the hash).
    """
    d = {k: v for k, v in case.items() if k != "expected_mask" and k not in _GAP_APPENDED_KEYS}
    prox = dict(d["proximity"])
    for k in _GAP_APPENDED_PROXIMITY_KEYS:
        prox.pop(k, None)
    d["proximity"] = prox
    return hashlib.sha256(json.dumps(d, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _round_half_even_div(numerator: int, denominator: int) -> int:
    """Banker's rounding of an exact integer quotient (never the production helper)."""
    q = (Decimal(numerator) / Decimal(denominator)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
    return int(q)


def _independent_cpr_bits(case: dict[str, Any]) -> int:
    """Recompute bits 60-63 from the case's own anchors — literal arithmetic.

    ``P = rhe((pdh+pdl+pdc)/3)``; ``BC = rhe((pdh+pdl)/2)``; ``TC = 2P − BC``;
    canonicalised ``tc = max``, ``bc = min``. Bit 60: strict ``close > tc``;
    bit 61: strict ``close < bc``; bit 62: inclusive ``bc <= close <= tc``;
    bit 63: ``p > 0`` and ``(tc − bc) * 10_000 <= p * 30``.
    """
    pdh = case["prev_pdh_paisa"]
    pdl = case["prev_pdl_paisa"]
    pdc = case["prev_pdc_paisa"]
    close = case["close_paisa"]
    p = _round_half_even_div(pdh + pdl + pdc, 3)
    bc_raw = _round_half_even_div(pdh + pdl, 2)
    tc_raw = 2 * p - bc_raw
    tc, bc = max(tc_raw, bc_raw), min(tc_raw, bc_raw)
    bits = 0
    if close > tc:
        bits |= 1 << 60
    if close < bc:
        bits |= 1 << 61
    if bc <= close <= tc:
        bits |= 1 << 62
    if p > 0 and (tc - bc) * 10_000 <= p * _NARROW_BPS:
        bits |= 1 << 63
    return bits


def _old_by_id() -> dict[int, dict[str, Any]]:
    return {c["id"]: c for c in _OLD["cases"]}


def test_companion_covers_the_full_corpus() -> None:
    """One companion row per new-fixture case — same ids, both 400 strong."""
    assert _OLD["schema"] == "vocab-golden-migration-v2-masks"
    assert len(_OLD["cases"]) == len(_NEW["cases"]) == 400
    assert {c["id"] for c in _OLD["cases"]} == {c["id"] for c in _NEW["cases"]}


def test_companion_records_the_pre_reshape_whole_file_sha() -> None:
    """The companion's provenance anchor is the OLD fixture's whole-file SHA-256.

    Pinned to the value independently re-derived from git history
    (``git show d6ac9f850:tests/golden/fixtures/vocab_golden_vectors.json |
    sha256sum``, verified 2026-07-15) — so the companion can never silently
    claim descent from a different pre-reshape corpus.
    """
    assert (
        _OLD["source_fixture_sha256"]
        == "19640054cb39e50bb33088dd31c496d69706e8cbfe0690e9f11ef86862831a3a"
    )


def test_inputs_are_byte_identical_to_the_pre_reshape_fixture() -> None:
    """The RNG-burn proof: every case's input fields carry the OLD fixture's SHA."""
    old = _old_by_id()
    mismatches = [c["id"] for c in _NEW["cases"] if _input_sha(c) != old[c["id"]]["inputs_sha256"]]
    assert not mismatches, f"case INPUTS drifted from the frozen pre-reshape corpus: {mismatches}"


def test_pre_existing_bits_are_back_compat_and_tombstones_cleared() -> None:
    """The back-compat harness: bits 0-51 + 54-59 identical; 52/53 cleared.

    ``new & ((1<<60)-1) == old & ~tombstones`` per case over the frozen inputs
    — i.e. every pre-existing ACTIVE condition evaluates bit-identically
    pre/post reshape, and the ONLY change inside the old 60-bit span is the
    tombstone clear.
    """
    old = _old_by_id()
    violations: list[str] = []
    tombstones_cleared = 0
    for case in _NEW["cases"]:
        new_mask = int(case["expected_mask"], 16)
        old_mask = int(old[case["id"]]["old_mask"], 16)
        if new_mask & _LOW60 != old_mask & ~_TOMBSTONE_MASK:
            violations.append(f"case {case['id']}: old={old_mask:#034x} new={new_mask:#034x}")
        if old_mask & _TOMBSTONE_MASK:
            tombstones_cleared += 1
    assert not violations, "old-vocabulary bit drift:\n" + "\n".join(violations[:20])
    # Anti-vacuity: the OLD corpus genuinely carried tombstone bits to clear.
    assert tombstones_cleared > 0


def test_no_bit_at_or_above_64_is_ever_set_except_the_gap_trio() -> None:
    """The declared-only SuperTrend bits (64/65) + reserved headroom stay inert.

    Since DEC-GAP-MIDPOINT-CONDITIONS-001 the IMPLEMENTED gap-midpoint bits
    66-68 legitimately fire past u64 — they are the ONLY sanctioned exception;
    bits 64/65 and every bit >= 69 must never be set.
    """
    gap_fired = 0
    for case in _NEW["cases"]:
        mask = int(case["expected_mask"], 16)
        stray = mask >> 64 << 64 & ~_GAP_MASK
        assert stray == 0, f"case {case['id']}: non-gap bit >= 64 fired ({mask:#034x})"
        if mask & _GAP_MASK:
            gap_fired += 1
    # Anti-vacuity: the gap trio genuinely fires somewhere in the corpus.
    assert gap_fired > 0


def test_new_cpr_bits_are_exactly_the_independent_arithmetic() -> None:
    """Bits 60-63 equal the in-test CPR recompute from the case's own anchors."""
    mismatches: list[str] = []
    fired = [0, 0, 0, 0]
    for case in _NEW["cases"]:
        mask = int(case["expected_mask"], 16)
        got = mask & (0xF << 60)
        expected = _independent_cpr_bits(case)
        if got != expected:
            mismatches.append(f"case {case['id']}: got={got:#x} expected={expected:#x}")
        for i in range(4):
            fired[i] += (mask >> (60 + i)) & 1
    assert not mismatches, "CPR bit drift from the arithmetic:\n" + "\n".join(mismatches[:20])
    # Anti-vacuity: every CPR bit fires somewhere AND is clear somewhere.
    n = len(_NEW["cases"])
    assert all(0 < f < n for f in fired), f"degenerate CPR coverage: {fired}"
