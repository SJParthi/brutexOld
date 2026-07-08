"""Golden byte-lock of the R33 phrase table (DEC-SWEEP-RESULTS-DEPTH-001, WP3).

The 60 plain-English phrases are hand-written ONCE and reviewed in PR (R33's
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
_GOLDEN_TABLE_SHA256 = "92f9df06fa5b2b5aa7401505e664630ad22b56b2f53e982b611bc4a766346e79"


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
    assert reason.technical == "near_pivot_r2 ∧ close_above_pdh ∧ close_above_vwap"
    assert reason.plain == (
        "price was near resistance level R2"
        "; and the candle closed above yesterday's high"
        "; and price was above the session's volume-weighted average price (VWAP)"
    )
