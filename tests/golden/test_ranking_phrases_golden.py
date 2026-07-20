"""Golden byte-lock of the R33 phrase table (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

The 74 plain-English phrases are hand-written ONCE and reviewed in PR (R33's
"written+reviewed once" law) — this digest makes any wording edit a
DELIBERATE golden re-lock, never a silent drift.

Regenerate (after a consciously reviewed wording change)::

    .venv/bin/python -c "
    import hashlib
    from brutex.ranking.phrases import PHRASE_TABLE
    canonical = '\\n'.join(f'{e.bit}|{e.name}|{e.plain}' for e in PHRASE_TABLE)
    print(hashlib.sha256(canonical.encode('utf-8')).hexdigest())
    "
"""

from __future__ import annotations

import hashlib

from brutex.ranking.phrases import PHRASE_TABLE, reason_for_mask

#: Locked golden digest of the canonical ``bit|name|plain`` serialization.
# DELIBERATE RE-LOCK (the vocab-66 SuperTrend flip, crate 0.5.0): the two
# SuperTrend rows (64/65) drop the "declared — math not yet active" prose
# for the live up/down-trend-hold wording (bit positions unchanged). Old
# digests: the 60→66 reshape:
# 221e39fef632f95cb3b5b4c206970c0f837249471adda8ecf9e4012139a45883;
# pre-reshape:
# 92f9df06fa5b2b5aa7401505e664630ad22b56b2f53e982b611bc4a766346e79.
# RE-LOCKED again at DEC-GAP-MIDPOINT-CONDITIONS-001 (woven onto the vocab-66
# SuperTrend flip at the train fold): the three Group-15 gap-midpoint entries
# (bits 66-68) were APPENDED after the ST rows — pre-existing rows
# byte-identical. Pre-gap (ST-era) digest:
# b4d8249ad59ef0f02d0355f5250ceb03bb8d58ee9f6cbdcbe549bfd27258b746.
# RE-LOCKED again at DEC-FIB-TEMPLATE-EXPRESS-001 (MIGRATION.md #1935, the
# fib-template express woven at the train fold): the 5 express rows —
# bull-236/786 retracements, the two-sided 424 extension, near-swing
# high/low (bits 69-73) — were APPENDED after the gap rows; the member's 3
# reserved gap-placeholder rows were DROPPED verbatim per the re-stack
# obligation (the REAL gap conditions hold 66-68). Every pre-existing row
# byte-identical (the 69-row prefix hashes to the pre-express gap-era
# digest). Pre-express (gap-era) digest:
# 7083320aec5c8726e6f5ffedab41780b57ca815103e68e51219ffb35952900ab.
# RE-LOCKED again at DEC-VOCAB-VWAP-RETAIN-001 (the operator ruling
# 10f5522e, woven at the train fold): the two VWAP rows (52/53) drop the
# "(VWAP; retired condition — never fires on current runs)" tombstone prose
# for the plain live wording — the vocab-66 wipe is REVERSED, bits 52/53 are
# ACTIVE again (bit positions + every OTHER row byte-identical). Pre-retain
# (tombstoned-VWAP-era) digest:
# e37de1cf9b15863344b481679877560b05073e0b16494e0516e10ac51eea075b.
_GOLDEN_TABLE_SHA256 = "475347f4d7f201b1970cb724b8ae60f036767b626c7436b076f08fc5d9173edb"


def test_phrase_table_digest_locked() -> None:
    canonical = "\n".join(f"{e.bit}|{e.name}|{e.plain}" for e in PHRASE_TABLE)
    assert hashlib.sha256(canonical.encode("utf-8")).hexdigest() == _GOLDEN_TABLE_SHA256, (
        "PHRASE_TABLE wording changed — if deliberate (reviewed), re-lock the digest "
        "per the module docstring; if not, revert the drift."
    )


def test_r33_reference_assembly_locked() -> None:
    # The R33 requirement's own worked example shape, locked end-to-end:
    # PDH-cross + above-R2 + above-VWAP as one dual-language sentence pair.
    mask = (1 << 13) | (1 << 8) | (1 << 52)
    reason = reason_for_mask(mask)
    # DELIBERATE RE-PIN (DEC-VOCAB-VWAP-RETAIN-001, the operator ruling
    # 10f5522e): the VWAP tombstone is REVERSED — bit 52 is ACTIVE again, so
    # describe.condition_display_name renders the plain name (no "(retired)"
    # suffix; RETIRED_MASK is now 0). Old tombstoned pin:
    # "near_pivot_r2 ∧ close_above_pdh ∧ close_above_vwap (retired)".
    assert reason.technical == "near_pivot_r2 ∧ close_above_pdh ∧ close_above_vwap"
    # The VWAP member renders the live plain prose (R33's own worked example).
    assert reason.plain == (
        "price was near resistance level R2"
        "; and the candle closed above yesterday's high"
        "; and price was above the session's volume-weighted average price (VWAP)"
    )
