"""The M4 pow-identity host probe (slice-4 PR-1) — shared test helper.

The Rust ranking kernels implement CPython's ``x ** 2`` / ``x ** 0.5``
(platform-libm ``pow``) as ``x * x`` / ``f64::sqrt``. The identities
``pow(x, 2) == x * x`` and ``pow(x, 0.5) == sqrt(x)`` are **MODELED, never
documented** (the slice-4 design's judge-corrected ruling — the "glibc pow
is correctly rounded" citation is RETRACTED): they are adjudicated per host
by THIS deterministic probe, and at runtime by the always-on per-call
differential (:mod:`brutex.engine.rust_parity_core`) that ships the Python
oracle's bits on any mismatch.

The probe is DETERMINISTIC (fixed-seed corpus + fixed adversarial values),
so a host's verdict is stable across runs — the gated strong test legs
(hard bit-equality differentials, zero-autorevert golden assertions) can
key on it without flake. Measured 2026-07-11 by THIS shipped probe
(``pow_identity_verdict()``: 4 regimes × ``_SAMPLES_PER_REGIME`` + the
adversarial values = 80,015 samples) on this repo's Linux/glibc build
container: the identity FAILS — 61 ``pow(x,2)`` + 65 ``pow(x,0.5)``
bit-mismatches over the 80,015 samples (an earlier, larger exploratory
200k-sample sweep that first caught the divergence is superseded by the
shipped probe's own figures — round-4 review honesty fix). On such hosts
the (b*) families serve oracle bits per call and MAY latch; on an identity
host the kernels are expected bit-identical.

MEASURED 2026-07-12 (coordinator-ruled amendment — MIGRATION.md #1731): the
production M4-Pro is NOT a pow-identity host either. verify-m4pro actions
run 29194011032 measured its Apple libm at 91 ``pow(x,2)`` + 123
``pow(x,0.5)`` bit-mismatches over the same 80,015-sample probe, so the
(b*) rows serve the Python oracle per call on EVERY measured host including
the reference hardware; the raw-kernel-bit-identity leg is documented as
not achievable there (the on-host ``powf`` transliteration alternative is
the deferred named follow-up DEC-RANKING-POWF-ONHOST-001 — no ledger entry
minted yet).
"""

from __future__ import annotations

import math
import random
import struct
from functools import lru_cache
from typing import NamedTuple

#: Deterministic probe corpus size per regime (kept modest — the probe runs
#: in well under a second; the hypothesis differentials add randomized
#: breadth on every CI run).
_SAMPLES_PER_REGIME = 20_000

#: The fixed probe seed — the verdict must be host-deterministic.
_SEED = 20_260_711

#: Adversarial fixed values: denormals, huge magnitudes, exact powers of
#: two (where the identity is exact by construction), near-1 values, and
#: the ranking kernels' own clamp/variance landmarks.
#: NOTE the corpus stays strictly below the square-overflow boundary
#: (|x| < ~1.34e154): CPython ``float.__pow__`` RAISES ``OverflowError``
#: where the hardware square would return ``inf``, so overflow-range inputs
#: have no identity to probe (the Python oracle raises there on BOTH arms —
#: behaviour-identical by the wrapper's oracle-call law).
_ADVERSARIAL: tuple[float, ...] = (
    5e-324,  # smallest subnormal (squares to 0.0 — underflow never raises)
    2.2250738585072014e-308,  # smallest normal
    1.3e154,  # just under the square-overflow boundary
    1.0,
    1.0 + 2**-52,
    1.0 - 2**-53,
    2.0,
    0.5,
    3.0,
    9.869604401089358,  # pi**2
    1e16,
    123456789.987654321,
    0.1,
    0.2,
    0.3,
)


class PowIdentityVerdict(NamedTuple):
    """The host's pow-identity probe outcome."""

    holds: bool
    square_mismatches: int
    sqrt_mismatches: int
    samples: int


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


@lru_cache(maxsize=1)
def pow_identity_verdict() -> PowIdentityVerdict:
    """Probe this host's libm for the two ranking pow identities.

    Returns the memoised deterministic verdict: ``holds`` is ``True`` iff
    ``x ** 2 == x * x`` AND ``|x| ** 0.5 == math.sqrt(|x|)`` bit-for-bit
    over the whole corpus. Non-finite outputs compare by bits too (``inf``
    squares agree trivially).
    """
    rng = random.Random(_SEED)
    values: list[float] = list(_ADVERSARIAL)
    # Uniform magnitudes across regimes: unit-scale, metric-scale (the
    # ranking inputs), large, tiny, and wide log-uniform.
    for _ in range(_SAMPLES_PER_REGIME):
        values.append(rng.uniform(-10.0, 10.0))
        values.append(rng.uniform(-1e6, 1e6))
        values.append(rng.uniform(0.0, 1.0))
        # Exponent capped at ±300: 2^301 squares to 2^602 (finite), while
        # anything near the 2^512 square boundary risks the CPython
        # OverflowError arm the corpus deliberately avoids (module note).
        values.append(math.ldexp(rng.uniform(1.0, 2.0), rng.randint(-300, 300)))
    square_bad = 0
    sqrt_bad = 0
    for x in values:
        if _bits(x**2) != _bits(x * x):
            square_bad += 1
        y = abs(x)
        if _bits(y**0.5) != _bits(math.sqrt(y)):
            sqrt_bad += 1
    return PowIdentityVerdict(
        holds=(square_bad == 0 and sqrt_bad == 0),
        square_mismatches=square_bad,
        sqrt_mismatches=sqrt_bad,
        samples=len(values),
    )


def pow_identity_holds() -> bool:
    """``True`` iff this host's libm satisfies both modeled pow identities."""
    return pow_identity_verdict().holds


__all__ = ["PowIdentityVerdict", "pow_identity_holds", "pow_identity_verdict"]
