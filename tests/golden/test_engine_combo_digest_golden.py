"""Golden byte-stability freeze for the sweep dedup digest.

DEC-ENGINE-DEDUP-NAN-CANON-001. :func:`brutex.engine.sweep_cache.combo_digest`
gained an ``allow_nan=False`` guard so the membership-dedup key obeys the same
one-NaN-policy as the run-identity hash + the completion sentinel (CLAUDE.md §2
Rule 11 COMMON). ``allow_nan=False`` only ever REJECTS a non-finite float — it
NEVER reshapes a finite value's JSON serialisation — so for any FINITE combo the
digest must be byte-for-byte identical to before the change.

This golden PINS the exact hex of two fixed FINITE combos (one carrying a finite
float param, exercising the very ``allow_nan=False`` code path; one with
``params=None``). The digests were frozen AFTER the guard landed and verified
equal to the pre-change digests — they are the byte-stability contract: any
future change to the canonicalisation (field order, ``sort_keys``, separators,
the BLAKE3 primitive, or the guard) that moves a FINITE digest is caught
byte-for-byte here (Golden Rule #4 reproducibility).

BLAKE3 ships in the wheel-free ``blake3`` package, so this golden runs anywhere
(no Rust wheel needed — see ``sweep_cache`` module docstring).
"""

from __future__ import annotations

from brutex.engine.sweep_cache import combo_digest

#: Frozen hex of a fixed FINITE combo carrying a FINITE float param
#: (``proximity=0.25``) — exercises the ``allow_nan=False`` path on a finite
#: value and proves it does NOT move the digest.
_GOLDEN_FINITE_FLOAT = "f4980d0fe164ce4200e73cf1ef40cd1cbb5db64a0ec27506164432b72a4d4143"

#: Frozen hex of a fixed combo with ``params=None`` — identical to the
#: pre-change digest (the ``{}`` empty-params branch is unaffected by the guard).
_GOLDEN_NONE = "220151bac87f37f9cf170f7c70f2d0be91ee56da9212a58c077ee811c1d581d1"


def test_combo_digest_finite_float_byte_stable() -> None:
    """A FINITE float param hashes to the frozen digest (allow_nan=False no-op)."""
    digest = combo_digest(
        0b1010110,
        "NIFTY",
        "5m",
        "bull",
        {"sl_paisa": 80, "tp_paisa": 50, "prior_n": 3, "proximity": 0.25},
    )
    assert digest.hex() == _GOLDEN_FINITE_FLOAT


def test_combo_digest_none_params_byte_stable() -> None:
    """The ``params=None`` combo hashes to the frozen digest (unchanged)."""
    digest = combo_digest(7, "BANKNIFTY", "1m", "bear", None)
    assert digest.hex() == _GOLDEN_NONE
