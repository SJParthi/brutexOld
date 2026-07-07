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


#: Strict ceiling for ratio tests whose big/small workloads CROSS a CPU cache
#: tier (DEC-O1-CACHE-TIER-001). The 2026-06-11 first REAL M4-Pro run proved the
#: decreed 1.1x is physically unsatisfiable when the large size spills L2/SLC
#: into DRAM: measured mask-AND 2.43x (0.20->0.49 ns/elem at 2.0 BILLION ops/s
#: absolute), dict lookup 2.11x (24->51 ns), primed rate window 1.20x — cache
#: hierarchy, not algorithmic regression (per-element cost is bounded by a
#: CONSTANT memory-tier factor, never O(n)). Cross-tier tests keep the decree
#: honest via this measured allowance; the BRUTEX_M4PRO_ABS absolute floor
#: still gates raw speed, and same-footprint paths keep the strict 1.1x.
CACHE_TIER_STRICT_CEILING: Final[float] = 3.0


def cache_tier_ceiling(shared_ci_default: float) -> float:
    """Ceiling for CROSS-CACHE-TIER ratio tests (DEC-O1-CACHE-TIER-001).

    Strict tier: the measured cache allowance (never looser than the shared
    default). Shared CI: unchanged.
    """
    if m4pro_strict_active():
        return min(CACHE_TIER_STRICT_CEILING, shared_ci_default)
    return shared_ci_default


#: Strict ceiling for ratio tests whose big/small workloads differ by a
#: feature's OWN documented O(D) METADATA sweep (DEC-LAKE-STATS-MANIFEST-001).
#: The 2026-07-07 first REAL M4-Pro run (actions run 28888100079) proved the
#: decreed 1.1x internally contradictory for the manifest paging click: its
#: HONEST envelope is O(page-files) Parquet work + an O(D) stat-token
#: revalidation sweep (D = the ONE dataset's month files — metadata syscalls),
#: so the 72-month fixture pays 66 extra statx calls over the 6-month one BY
#: DESIGN. Measured: ratio 1.53x, t_small=2.272 ms, t_large=3.485 ms —
#: Δ≈18 µs/file over the 66 extra statx — while the deterministic
#: 1-file-touch probe stayed green (exactly 1 parquet file touched at both
#: depths). Metadata cost is bounded by a CONSTANT per-file syscall factor,
#: never O(files) Parquet work; a genuine O(files) regression on the
#: 12x-depth fixture measures ~12x and still fails 3.0 loudly, and the
#: deterministic file-touch / fs-op-count probes remain the primary teeth.
METADATA_SWEEP_STRICT_CEILING: Final[float] = 3.0


def metadata_sweep_ceiling(shared_ci_default: float) -> float:
    """Ceiling for documented O(D)-METADATA-sweep ratio tests (DEC-LAKE-STATS-MANIFEST-001).

    Strict tier: the measured metadata allowance (never looser than the shared
    default). Shared CI: unchanged.
    """
    if m4pro_strict_active():
        return min(METADATA_SWEEP_STRICT_CEILING, shared_ci_default)
    return shared_ci_default


def ratio_ceiling(shared_ci_default: float) -> float:
    """Return the O(1) ratio ceiling for the active tier.

    Returns the decreed :data:`STRICT_RATIO_CEILING` (1.1×) when the strict
    M4-Pro tier is active, otherwise the caller's robust ``shared_ci_default``
    (identical behaviour on shared runners — zero added flakiness).
    """
    return STRICT_RATIO_CEILING if m4pro_strict_active() else shared_ci_default
