"""O(1) benchmark for the until-clean controller decision (DEC-TOKEN-PREFLIGHT-001).

:func:`~brutex.adapters.ingest_common.decide_until_clean` and
:func:`~brutex.adapters.dhan.token_preflight.classify_token_lifetime` are PER-CALL
O(1) — a constant number of comparisons whose cost must be **independent** of the
residual magnitude / how far behind the loop fell / the token's remaining life. A
verdict whose cost scaled with the residual count would betray a hidden O(N) scan.

Strategy: time the decision with a tiny residual/lifetime versus an enormous one;
O(1) ⇒ ratio ≈ 1.0. The ceiling routes through the shared two-tier
:func:`tests.bench._o1_tiers.ratio_ceiling` (1.1× strict on the M4-Pro runner, a
robust 5× on the noisy shared runner).

Honest envelope (Golden Rule #10): this proves the *decision* is O(1) per call; it
says NOTHING about the pull the loop drives, whose work is ``O(Σ failed_windows)``
per round across a bounded round count — never an "O(1)-total" claim.
"""

from __future__ import annotations

import time

from brutex.adapters.dhan.token_preflight import classify_token_lifetime
from brutex.adapters.ingest_common import decide_until_clean

from tests.bench._o1_tiers import ratio_ceiling

_ITERATIONS = 200_000


def _time(call: object, *, repeat: int = _ITERATIONS) -> float:
    """Return mean ns/call for *call* (a zero-arg lambda), after warming caches."""
    fn = call  # type: ignore[assignment]
    for _ in range(1_000):
        fn()  # type: ignore[operator]
    t0 = time.perf_counter_ns()
    for _ in range(repeat):
        fn()  # type: ignore[operator]
    t1 = time.perf_counter_ns()
    return (t1 - t0) / repeat


def test_decide_until_clean_is_constant_time() -> None:
    """Per-call cost must not grow with the residual magnitude."""
    t_small = _time(
        lambda: decide_until_clean(
            still_failed_windows=1, auth_aborted=False, rounds_used=1, max_rounds=5
        )
    )
    t_huge = _time(
        lambda: decide_until_clean(
            still_failed_windows=10_000_000,
            auth_aborted=False,
            rounds_used=1,
            max_rounds=5,
        )
    )
    ratio = t_huge / max(t_small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"decide_until_clean cost scales with residual magnitude: ratio {ratio:.2f}×. "
        f"t_small={t_small:.0f}ns, t_huge={t_huge:.0f}ns."
    )


def test_classify_lifetime_is_constant_time() -> None:
    """Token classification cost must not grow with the remaining-life magnitude."""
    t_small = _time(lambda: classify_token_lifetime(1.0))
    t_huge = _time(lambda: classify_token_lifetime(1e12))
    ratio = t_huge / max(t_small, 1.0)
    assert ratio < ratio_ceiling(5.0), (
        f"classify_token_lifetime cost scales with lifetime magnitude: ratio {ratio:.2f}×. "
        f"t_small={t_small:.0f}ns, t_huge={t_huge:.0f}ns."
    )
