"""The vocab-reshape migration invariants — the permanent back-compat harness.

TWO chained frozen-input migrations over the 400-case oracle
``fixtures/vocab_golden_vectors.json`` (GOLD-01 — never a free
regenerate-and-trust):

**v2→v3→v4 (VWAP RETAINED — DEC-VOCAB-VWAP-RETAIN-001, the operator ruling
10f5522e woven at the vocab train):** the vocab-66 wipe TOMBSTONED VWAP bits
52/53 at v3; the retention REVERSES that tombstone — bits 52/53 are ACTIVE
again, and the v4 generator FEEDS the (formerly burned) VWAP RNG draw into the
snapshot, so the v4 fixture's 52/53 come BACK to their pre-reshape v2 values
(the restored VWAP math is main's verbatim). The v2-era INPUT fields stay
byte-identical to the pre-reshape (version-2) fixture — pinned per case via the
committed ``fixtures/vocab_golden_migration_v2_masks.json`` companion. Per case
the SPLIT-LAW is: ``new & ((1<<60)-1) == old_v2 & ((1<<60)-1)`` (FULL low-60
identity — the tombstone clear is REVERSED, 52/53 restored to their v2 values;
the v2 companion carries 355 of 400 cases with 52/53 firing), and bits 60-63
are exactly the CPR arithmetic recomputed INDEPENDENTLY here from the case's
own anchors. NOTE: the v4 fixture stays RED against this restored law until
the Regen phase regenerates ``vocab_golden_vectors.json`` with 52/53 firing —
this pass adjusts the LAW only (no fixture regeneration; GOLD-01).

**v3→v4 (the vocab-66 SuperTrend implemented-flip, crate 0.5.0):** bits 64/65
went live. The v4 generator APPENDS the ``supertrend_x2_paisa`` draw strictly
AFTER the ENTIRE historical draw stream (all 400 cases' v3 draws are consumed
untouched first), so every v3-era input field is byte-identical — pinned per
case via the committed ``fixtures/vocab_golden_migration_v3_masks.json``
companion (input SHA over the case dict minus ``expected_mask`` AND minus the
NEW ``supertrend_x2_paisa`` field) — and per case the SPLIT-LAW under VWAP
retention is ``new & ((1<<64)-1) & ~TOMB == old_v3 & ~TOMB`` (bits 0-63 EXCEPT
the tombstones bit-identical to v3) PLUS ``new & TOMB == old_v2 & TOMB`` (the
52/53 bits come back to their v2/pre-reshape values, NOT v3's cleared zeros —
v3 carried 0 of 400 with 52/53 firing, so the retention re-fires them). The
SuperTrend bits (64/65) are exactly the strict ×2-domain comparison recomputed
INDEPENDENTLY here (``close×2`` vs the case's own ``supertrend_x2_paisa``;
``None`` / equality → both clear). The split-law is self-consistent: within
the low-60 span minus the tombstones, v2 and v3 agree byte-for-byte on all 400
cases (verified — the reshape touched only 52/53 in that span), so the two
invariants above never contradict. No bit at or above 66 is ever set by the
v4 layer itself.

**DEC-GAP-MIDPOINT-CONDITIONS-001 (the 66→69 growth, woven at the vocab
train):** the gap-midpoint bits 66-68 were APPENDED with exactly two per-case
input keys drawn from a SEPARATE second RNG stream — the pre-existing
invariants above still hold verbatim over the pre-existing fields/bits (the
appended keys are enumerated and excluded from the input hash; the gap bits
were, pre-express, the highest sanctioned firing class).

**DEC-FIB-TEMPLATE-EXPRESS-001 (the 69→74 express, woven at the vocab
train):** the fib-template express bits 69-73 were APPENDED implemented; the
400 seeded cases keep their key set BYTE-IDENTICALLY (no swing keys — readers
default them) and the DETERMINISTIC express cases (ids >= 400) carry the new
swing inputs, drawn from NO RNG — so both frozen streams above are untouched
and the seeded-corpus invariants are scoped via ``_migration_cases()``
(ids < 400). Bits 69-71 derive from the frozen prev-day anchors, so they may
legitimately fire on seeded cases too; everything >= 74 stays inert.

Each companion is a committed copy of its era's masks: the ``note`` field
records the provenance prose and ``source_fixture_sha256`` the source
fixture's WHOLE-FILE SHA-256 (v2: ``19640054cb39…`` verified against
``git show d6ac9f850:…``; v3: ``0f0f5f85b299…`` — the exact
``_EXPECTED_SHA256`` the pre-flip ``test_vocab_golden.py`` pinned), so the
chain stays verifiable forever — a later regeneration that silently moved an
input or a settled-vocabulary bit fails loudly here.
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
_OLD_V3 = json.loads(
    (_FIXTURE_DIR / "vocab_golden_migration_v3_masks.json").read_text(encoding="utf-8")
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
#: The pre-SuperTrend-flip 64-bit span (bits 0-63 — frozen across v3→v4).
_LOW64 = (1 << 64) - 1
#: The narrow-CPR width threshold (basis points of P) — restated literally
#: (the cross-pin against the wheel export lives in the vocabulary unit suite).
_NARROW_BPS = 30


def _input_sha(case: dict[str, Any]) -> str:
    """SHA-256 of a case's PRE-v4-era, PRE-gap INPUT fields.

    Everything except ``expected_mask``, the v4-appended
    ``supertrend_x2_paisa`` field AND the DEC-GAP-MIDPOINT-CONDITIONS-001
    appends (``gap_mid_paisa`` + ``proximity.gap_mid_bps`` — the enumerated
    ``_GAP_APPENDED_KEYS``) — canonical JSON (``sort_keys`` + compact
    separators), exactly how BOTH companions' ``inputs_sha256`` values were
    minted from their era's fixture (the v2 and v3 fixtures carried identical
    input fields — the v2→v3 regen burned the VWAP draw in place; v4 and the
    gap growth only APPENDED fields, so stripping them recovers the frozen
    v2/v3 input dict byte-for-byte). A NEW key outside the enumerated
    append-set still fails loudly (it lands in the hash).
    """
    d = {
        k: v
        for k, v in case.items()
        if k not in ("expected_mask", "supertrend_x2_paisa") and k not in _GAP_APPENDED_KEYS
    }
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


def _migration_cases() -> list[dict[str, Any]]:
    """The 400 seeded migration-invariant cases (ids 0-399).

    DEC-FIB-TEMPLATE-EXPRESS-001 appended DETERMINISTIC express cases at
    ids >= 400 (they carry NEW swing inputs, so no pre-reshape companion row
    can exist for them); the migration invariant is scoped to the frozen
    seeded corpus the companion fixture describes.
    """
    return [c for c in _NEW["cases"] if c["id"] < 400]


def test_companion_covers_the_full_corpus() -> None:
    """One companion row per seeded case — same ids, both 400 strong."""
    assert _OLD["schema"] == "vocab-golden-migration-v2-masks"
    migration = _migration_cases()
    assert len(_OLD["cases"]) == len(migration) == 400
    assert {c["id"] for c in _OLD["cases"]} == {c["id"] for c in migration}


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
    """The RNG-burn proof: every seeded case's input fields carry the OLD SHA.

    Strongest form under DEC-FIB-TEMPLATE-EXPRESS-001: the 400 seeded cases
    carry NO swing keys at all (readers default them), so the hash covers the
    LITERAL historical key set — any added/renamed/reordered input key fails
    here, including a swing key accidentally leaking into a seeded case.
    """
    old = _old_by_id()
    mismatches = [
        c["id"] for c in _migration_cases() if _input_sha(c) != old[c["id"]]["inputs_sha256"]
    ]
    assert not mismatches, f"case INPUTS drifted from the frozen pre-reshape corpus: {mismatches}"


def test_pre_existing_bits_are_back_compat_and_tombstones_restored() -> None:
    """The back-compat harness: FULL low-60 identity (VWAP 52/53 RESTORED).

    ``new & ((1<<60)-1) == old_v2 & ((1<<60)-1)`` per case over the frozen
    inputs — i.e. every pre-existing ACTIVE condition evaluates bit-identically
    pre-reshape/post-retention, INCLUDING the VWAP bits 52/53, which come BACK
    to their pre-reshape v2 values (DEC-VOCAB-VWAP-RETAIN-001 reverses the
    vocab-66 tombstone; the restored VWAP math is main's verbatim).
    """
    old = _old_by_id()
    violations: list[str] = []
    tombstones_restored = 0
    for case in _migration_cases():
        new_mask = int(case["expected_mask"], 16)
        old_mask = int(old[case["id"]]["old_mask"], 16)
        if new_mask & _LOW60 != old_mask & _LOW60:
            violations.append(f"case {case['id']}: old={old_mask:#034x} new={new_mask:#034x}")
        if old_mask & _TOMBSTONE_MASK:
            tombstones_restored += 1
    assert not violations, "old-vocabulary bit drift:\n" + "\n".join(violations[:20])
    # Anti-vacuity: the v2 corpus genuinely carried VWAP bits to restore.
    assert tombstones_restored > 0


#: The five DEC-FIB-TEMPLATE-EXPRESS-001 express bits (69-73) — the newest
#: sanctioned high-bit firing class on the woven train (beside the SuperTrend
#: pair 64/65 and the gap trio 66-68); everything >= 74 must stay inert.
_EXPRESS_HIGH_MASK = 0b11111 << 69


def test_no_bit_at_or_above_74_is_ever_set() -> None:
    """The reserved headroom (bits >= 74) stays inert.

    DELIBERATE law changes: the vocab-66 SuperTrend implemented-flip
    (crate 0.5.0) retired the former ``>> 64 == 0`` law — bits 64/65 MAY now
    fire (their exact justification is
    :func:`test_new_supertrend_bits_are_exactly_the_independent_arithmetic`);
    DEC-GAP-MIDPOINT-CONDITIONS-001 retired ``>> 66 == 0`` — the IMPLEMENTED
    gap trio 66-68 MAY fire (its VERBATIM-carried level is exercised by the
    vocab golden + Python<->Rust parity suites); and
    DEC-FIB-TEMPLATE-EXPRESS-001 retired ``>> 69 == 0`` — the express bits
    69-73 MAY fire (69-71 derive from the frozen anchors, so they fire on
    seeded cases too; 72/73 need the express cases' swing inputs). Everything
    at or above the declared 74-bit layout must never fire.
    """
    gap_fired = 0
    seen_express = 0
    for case in _NEW["cases"]:
        mask = int(case["expected_mask"], 16)
        assert mask >> 74 == 0, f"case {case['id']}: bit >= 74 fired ({mask:#034x})"
        if mask & _GAP_MASK:
            gap_fired += 1
        seen_express |= mask & _EXPRESS_HIGH_MASK
    # Anti-vacuity: the gap trio genuinely fires somewhere in the corpus, and
    # every express bit fires somewhere (both-ways coverage guaranteed by the
    # deterministic express cases at ids >= 400).
    assert gap_fired > 0
    assert seen_express == _EXPRESS_HIGH_MASK


# ────────────────────────────────────────────────────────────────────────
# v3→v4 — the vocab-66 SuperTrend implemented-flip (crate 0.5.0)
# ────────────────────────────────────────────────────────────────────────


def _independent_supertrend_bits(case: dict[str, Any]) -> int:
    """Recompute bits 64/65 from the case's own ST input — literal arithmetic.

    The strict ×2-domain law (never the production evaluator): with
    ``line = supertrend_x2_paisa`` (already ×2-scaled) and
    ``close_x2 = close_paisa * 2`` — bit 64 iff ``close_x2 > line``; bit 65
    iff ``close_x2 < line``; ``None`` or exact equality → both clear.
    """
    line = case["supertrend_x2_paisa"]
    if line is None:
        return 0
    close_x2 = case["close_paisa"] * 2
    if close_x2 > line:
        return 1 << 64
    if close_x2 < line:
        return 1 << 65
    return 0


def _old_v3_by_id() -> dict[int, dict[str, Any]]:
    return {c["id"]: c for c in _OLD_V3["cases"]}


def test_v3_companion_covers_the_seeded_corpus() -> None:
    """One v3-companion row per SEEDED case — same ids, both 400 strong.

    The DEC-FIB-TEMPLATE-EXPRESS-001 deterministic express cases (ids >= 400)
    post-date the v3 companion, so the invariant is scoped to the frozen
    seeded corpus exactly like the v2 chain (``_migration_cases``).
    """
    migration = _migration_cases()
    assert _OLD_V3["schema"] == "vocab-golden-migration-v3-masks"
    assert len(_OLD_V3["cases"]) == len(migration) == 400
    assert {c["id"] for c in _OLD_V3["cases"]} == {c["id"] for c in migration}


def test_v3_companion_records_the_pre_flip_whole_file_sha() -> None:
    """The v3 companion's provenance anchor is the v3 fixture's whole-file SHA.

    Pinned to the exact ``_EXPECTED_SHA256`` the pre-flip
    ``test_vocab_golden.py`` carried for the version-3 fixture (the committed
    bytes the companion was minted from, immediately before the v4 regen) —
    so the companion can never silently claim descent from a different
    pre-flip corpus.
    """
    assert (
        _OLD_V3["source_fixture_sha256"]
        == "0f0f5f85b29941beff20ec71a7e4942bce681d80e3c8f3c313e28da2445057a0"
    )


def test_v3_inputs_are_byte_identical_to_the_pre_flip_fixture() -> None:
    """The appended-draw proof: stripping the NEW fields recovers the v3 inputs."""
    old = _old_v3_by_id()
    mismatches = [
        c["id"] for c in _migration_cases() if _input_sha(c) != old[c["id"]]["inputs_sha256"]
    ]
    assert not mismatches, f"case INPUTS drifted from the frozen v3 corpus: {mismatches}"


def test_every_seeded_case_carries_the_new_supertrend_field() -> None:
    """v4 shape: every SEEDED case has ``supertrend_x2_paisa`` (int or None).

    (The DEC-FIB-TEMPLATE-EXPRESS-001 express cases at ids >= 400 omit the
    key by design — readers default it exactly like the Rust serde twin.)
    """
    for case in _migration_cases():
        assert "supertrend_x2_paisa" in case, f"case {case['id']} lacks the v4 field"
        value = case["supertrend_x2_paisa"]
        assert value is None or isinstance(value, int)


def test_pre_flip_bits_are_back_compat_across_the_supertrend_flip() -> None:
    """The v3→v4 SPLIT-LAW harness (VWAP RETAINED — DEC-VOCAB-VWAP-RETAIN-001).

    ``new & ((1<<64)-1) & ~TOMB == old_v3 & ~TOMB`` over the frozen inputs —
    every settled condition OUTSIDE the tombstones (the CPR span + SuperTrend +
    everything 0-51/54-63) evaluates bit-identically pre/post flip — PLUS
    ``new & TOMB == old_v2 & TOMB`` — the VWAP bits 52/53 come back to their
    pre-reshape v2 values (v3 had them cleared; retention re-fires them). The
    ONLY change above bit 63 is the SuperTrend layer.
    """
    old_v3 = _old_v3_by_id()
    old_v2 = _old_by_id()
    violations: list[str] = []
    tombstones_restored = 0
    for case in _migration_cases():
        new_mask = int(case["expected_mask"], 16)
        old_v3_mask = int(old_v3[case["id"]]["old_mask"], 16)
        old_v2_mask = int(old_v2[case["id"]]["old_mask"], 16)
        if new_mask & _LOW64 & ~_TOMBSTONE_MASK != old_v3_mask & ~_TOMBSTONE_MASK:
            violations.append(
                f"case {case['id']} (non-tomb): v3={old_v3_mask:#034x} new={new_mask:#034x}"
            )
        if new_mask & _TOMBSTONE_MASK != old_v2_mask & _TOMBSTONE_MASK:
            violations.append(
                f"case {case['id']} (tomb): v2={old_v2_mask:#034x} new={new_mask:#034x}"
            )
        if old_v2_mask & _TOMBSTONE_MASK:
            tombstones_restored += 1
    assert not violations, "settled-vocabulary bit drift:\n" + "\n".join(violations[:20])
    # Anti-vacuity: the v2 corpus genuinely carried VWAP bits the retention re-fires.
    assert tombstones_restored > 0


def test_new_supertrend_bits_are_exactly_the_independent_arithmetic() -> None:
    """Bits 64/65 equal the in-test strict ×2 recompute from the case's own input."""
    mismatches: list[str] = []
    fired = [0, 0]
    none_cases = 0
    equality_cases = 0
    migration = _migration_cases()
    for case in migration:
        mask = int(case["expected_mask"], 16)
        got = mask & (0b11 << 64)
        expected = _independent_supertrend_bits(case)
        if got != expected:
            mismatches.append(f"case {case['id']}: got={got:#x} expected={expected:#x}")
        for i in range(2):
            fired[i] += (mask >> (64 + i)) & 1
        if case["supertrend_x2_paisa"] is None:
            none_cases += 1
        elif case["supertrend_x2_paisa"] == case["close_paisa"] * 2:
            equality_cases += 1
    assert not mismatches, "SuperTrend bit drift from the arithmetic:\n" + "\n".join(
        mismatches[:20]
    )
    # Anti-vacuity: both bits fire somewhere AND are clear somewhere, and the
    # None (warm-up) + exact-equality both-clear arms are genuinely present.
    n = len(migration)
    assert all(0 < f < n for f in fired), f"degenerate SuperTrend coverage: {fired}"
    assert none_cases > 0, "no warm-up (None) SuperTrend case in the corpus"
    assert equality_cases > 0, "no exact-equality SuperTrend case in the corpus"


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
