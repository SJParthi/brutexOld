"""The pow-identity HOST PROBE (slice-4 PR-1 host-adjudication test).

Adjudicates the two MODELED identities the Rust ranking kernels rest on —
``x ** 2 == x * x`` and ``x ** 0.5 == math.sqrt(x)`` (CPython routes both
``**`` forms through the platform libm ``pow``; the kernels use the
deterministic ``x * x`` / ``f64::sqrt``) — over the deterministic
:mod:`tests.fakes.pow_identity` corpus plus a hypothesis-randomized sweep.

The two-arm law (the slice-4 design §3.4):

* **Identity host**: the probe asserts HARD — zero mismatches. NO measured
  host currently qualifies: the production M4-Pro itself MEASURED
  non-identity (91 + 123 mismatches per 80,015 samples, verify-m4pro
  actions run 29194011032, coordinator-ruled 2026-07-12 — the MIGRATION.md
  #1731 amendment).
* **Non-identity host** (glibc — measured live on this repo's build
  container — AND the production M4-Pro's Apple libm, per the run above):
  the probe SKIPS with the mismatch counts in the skip reason after
  emitting one ERROR-level structlog event — loud, never a silent
  assumption, and never a red gate on a host where the runtime is already
  protected by the ALWAYS-ON per-call differential (ship-the-oracle +
  autorevert; :mod:`brutex.engine.rust_parity_core`). Correctness is
  intact there; only the one-language-consistency payoff is unrealized —
  the ruled PERMANENT posture: the (b*) rows serve the Python oracle per
  call on every host, and the on-host ``powf`` transliteration alternative
  is the deferred named follow-up DEC-RANKING-POWF-ONHOST-001.
"""

from __future__ import annotations

import math
import struct

import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes.pow_identity import pow_identity_holds, pow_identity_verdict

_LOG = structlog.get_logger("tests.pow_host_probe")


def _bits(x: float) -> bytes:
    return struct.pack("<d", x)


def test_pow_identity_probe_two_arm_verdict() -> None:
    """HARD-assert the identities on an identity host; skip LOUDLY elsewhere."""
    verdict = pow_identity_verdict()
    if verdict.holds:
        assert verdict.square_mismatches == 0
        assert verdict.sqrt_mismatches == 0
        return
    # The skip-with-ERROR-visibility arm: the (b*) rows are served by the
    # Python oracle on any divergent call here (always-on differential), so
    # a red gate would punish a host the design already handles — but the
    # condition must be LOUD, never silently assumed away.
    _LOG.error(
        "pow_host_probe.identity_failed",
        square_mismatches=verdict.square_mismatches,
        sqrt_mismatches=verdict.sqrt_mismatches,
        samples=verdict.samples,
    )
    pytest.skip(
        "host libm pow is NOT the x*x/sqrt identity "
        f"({verdict.square_mismatches} square + {verdict.sqrt_mismatches} sqrt "
        f"mismatches over {verdict.samples} samples) — the (b*) ranking rows "
        "serve the Python oracle per call on this host (always-on "
        "differential); the strong bit-identity legs are gated off. The "
        "production M4-Pro itself measured non-identity (run 29194011032, "
        "coordinator-ruled 2026-07-12) — oracle-served is the permanent "
        "posture; on-host powf is the deferred DEC-RANKING-POWF-ONHOST-001 "
        "follow-up."
    )


def test_probe_verdict_is_deterministic_and_memoised() -> None:
    """Same process → same verdict object (fixed seed + lru_cache)."""
    assert pow_identity_verdict() is pow_identity_verdict()
    assert pow_identity_holds() is pow_identity_verdict().holds


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=-1e150, max_value=1e150))
def test_square_identity_randomized(x: float) -> None:
    """Randomized breadth on identity hosts (auto-skipped elsewhere)."""
    if not pow_identity_holds():  # pragma: no cover - host-dependent arm
        return
    assert _bits(x**2) == _bits(x * x)


@given(st.floats(allow_nan=False, allow_infinity=False, min_value=0.0, max_value=1e300))
def test_sqrt_identity_randomized(x: float) -> None:
    """Randomized breadth on identity hosts (auto-skipped elsewhere)."""
    if not pow_identity_holds():  # pragma: no cover - host-dependent arm
        return
    assert _bits(x**0.5) == _bits(math.sqrt(x))


def test_exact_power_of_two_identities_hold_on_every_host() -> None:
    """The EXACTLY-REPRESENTABLE shapes hold unconditionally.

    ``(2**e) ** 2 == 2**(2e)`` for every ``e`` and ``(2**e) ** 0.5 ==
    2**(e/2)`` for EVEN ``e`` land on exactly-representable powers of two —
    a floor even a faithfully-rounded (~1-ulp) libm meets, because when the
    true result IS a float both faithful neighbours coincide with it. These
    are the shapes the strong golden legs rest on (the ``_exact_pool``
    construction keeps every z-score intermediate exact), so they assert
    HARD on every host. The sqrt of an ODD power of two is IRRATIONAL
    (``sqrt(8.0)`` is not a float), so ``x ** 0.5 == math.sqrt(x)`` there
    is the host-dependent (b*) identity the shipped probe adjudicates — it
    is asserted only on identity hosts, in the gated sibling test below,
    NEVER here (on this repo's glibc container the probe measures that
    exact identity FAILING; any agreement on these 101 points would be
    coincidence, not a floor).
    """
    for e in range(-50, 51):
        x = math.ldexp(1.0, e)
        assert _bits(x**2) == _bits(x * x)
        if e % 2 == 0:
            assert _bits(x**0.5) == _bits(math.sqrt(x))
    assert _bits(1.0**2) == _bits(1.0 * 1.0)
    assert _bits(0.0**0.5) == _bits(math.sqrt(0.0))


def test_odd_exponent_sqrt_identity_gated_on_identity_hosts() -> None:
    """The IRRATIONAL-sqrt half of the old unconditional loop, host-gated.

    ``sqrt(2**odd)`` is irrational, so ``x ** 0.5 == math.sqrt(x)`` on
    these points is exactly the modeled (b*) identity — host-dependent by
    the two-arm law, asserted HARD only where the probe says the identity
    holds (NO measured host so far: the glibc build container AND the
    production M4-Pro both measured non-identity — run 29194011032) and
    skipped LOUDLY elsewhere, mirroring the randomized legs above.
    """
    if not pow_identity_holds():
        pytest.skip(
            "host libm pow is NOT the sqrt identity — odd-exponent "
            "power-of-two sqrt is host-dependent (the (b*) arm); covered at "
            "runtime by the always-on differential."
        )
    for e in range(-49, 50, 2):
        x = math.ldexp(1.0, e)
        assert _bits(x**0.5) == _bits(math.sqrt(x))
