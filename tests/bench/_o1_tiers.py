"""Two-tier O(1) ratio ceiling — the single source of truth (DEC-O1-STRICT-TIER-001).

The O(1) NFR decree (CLAUDE.md §2 r5 / §12) targets a
``p99(N_big)/p99(N_small) ≤ 1.1×`` latency ratio on every hot path. On the
shared 2-core GitHub runners that ratio is too noisy to gate at 1.1× without
flaking (DEC-P0-020), so each O(1) bench gate enforces a **robust shared-CI
ceiling** (3–10×) by default. The decreed strict **1.1×** is enforced as a
*second tier* on the dedicated, quiet self-hosted **M4-Pro** runner, where
``BRUTEX_M4PRO_STRICT=1`` is set (mirrors the ``BRUTEX_M4PRO_ABS`` absolute-gate
pattern in ``.github/workflows/bench-m4pro.yml``).

This keeps shared CI green-and-non-flaky (zero behaviour change when the env var
is unset) while making the 1.1× decree a real, machine-checked gate on hardware
that can hold it. Every O(1) ratio assertion routes its ceiling through
:func:`ratio_ceiling` so the tier is defined in exactly one place (CLAUDE.md §11
COMMON).
"""

from __future__ import annotations

import os
from typing import Final

#: Env flag set ONLY on the self-hosted M4-Pro runner (bench-m4pro.yml).
ENV_M4PRO_STRICT: Final[str] = "BRUTEX_M4PRO_STRICT"

#: The decreed strict O(1) latency-ratio ceiling (CLAUDE.md §2 r5 / §12).
STRICT_RATIO_CEILING: Final[float] = 1.1


def m4pro_strict_active() -> bool:
    """Return ``True`` iff the strict 1.1× O(1) tier is enabled (M4-Pro runner)."""
    return os.environ.get(ENV_M4PRO_STRICT, "").strip() == "1"


def ratio_ceiling(shared_ci_default: float) -> float:
    """Return the O(1) ratio ceiling for the active tier.

    Returns the decreed :data:`STRICT_RATIO_CEILING` (1.1×) when the strict
    M4-Pro tier is active, otherwise the caller's robust ``shared_ci_default``
    (identical behaviour on shared runners — zero added flakiness).
    """
    return STRICT_RATIO_CEILING if m4pro_strict_active() else shared_ci_default
